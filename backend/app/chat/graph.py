"""主对话 LangGraph — 7 节点 + 1 条件路由（4 分支）。

图拓扑（M9 主体最终态，patch0 锁死）：
    START → load_audit_state → route_by_risk
    ├─ crisis  → build_messages_crisis  → call_crisis_llm  → END
    ├─ redline → build_messages_redline → call_redline_llm → END
    ├─ guidance→ build_messages_main    → call_main_llm    → END
    └─ main    → build_messages_main    → call_main_llm    → END

周知（T5）：
  - persist_ai_turn / enqueue_audit 为顶层 helper（me.py generator 调用），不在图内
  - me.py generator 是单写入点（T5 single-write-point）
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
)
from langchain_core.runnables import Runnable
from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import select

from app.chat.context import (
    build_crisis_context,
    build_redline_context,
    load_active_history_for_assembly,
)
from app.chat.context_schema import ChatContextSchema
from app.chat.prompts import (
    build_crisis_system_prompt,
    build_redline_system_prompt,
    build_system_prompt,
    format_guidance_wrapper,
    format_reentry_wrapper_crisis,
    format_reentry_wrapper_redline,
)
from app.chat.state import AuditState, MainDialogueState
from app.core.llm import build_crisis_llm, build_main_llm, build_redline_llm
from app.core.llm_extractors import extract_finish_reason, extract_reasoning_content, extract_usage
from app.domain.audit.signals import AuditSignalsManager
from app.models.audit import RollingSummary

if TYPE_CHECKING:
    from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


async def _pg_crisis_fallback(ctx) -> dict:
    """查 PG rolling_summaries.crisis_locked_message_id（内部开闭 db session）。

    (b) 路径：ready 和降级分支一致查 PG，rolling_summaries 是 sticky lock 单一真相源。
    """
    async with ctx.db_session_factory() as db:
        rs = await db.scalar(
            select(RollingSummary).where(RollingSummary.session_id == ctx.session_id).limit(1)
        )
    locked_id = rs.crisis_locked_message_id if rs else None
    return {"crisis_locked": locked_id is not None, "target_message_id": locked_id}


async def load_audit_state(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    """加载审查信号：Redis poll_wait + PG crisis_locked 粘性兜底（M9 双源）。

    分支拓扑：
       turn==1       → 早退 all-False（理由：首轮 rs 行不存在，PG 查询纯浪费）
       ready         → Redis 当轮信号 + PG 粘性 crisis_locked + guidance or None
       failed/miss/... → PG 粘性 crisis_locked + 当轮 all-False + 日志

    PG 查询 _pg_crisis_fallback 在 ready 和降级分支都执行，(b) 单一来源。
    """
    ctx = runtime.context
    turn = state.get("turn_number", 1)

    if turn == 1:
        return _all_false_audit_state()

    sid = str(ctx.session_id)
    manager = AuditSignalsManager(
        ctx.audit_redis,
        ttl=ctx.settings.audit_redis_ttl_seconds,
    )
    result = await manager.poll_wait(
        sid,
        expected_turn=turn - 1,
        timeout=ctx.settings.audit_wait_timeout_seconds,
    )

    if result.kind == "ready" and result.signals is not None:
        logger.info("audit.load.ready sid=%s turn=%s", sid, turn)
        pg_fb = await _pg_crisis_fallback(ctx)
        return {
            "audit_state": {
                "crisis_locked": pg_fb["crisis_locked"],
                "crisis_detected": result.signals.crisis_detected,
                "redline_triggered": result.signals.redline_triggered,
                "guidance": result.signals.guidance_injection or None,
                "target_message_id": pg_fb["target_message_id"],
            },
        }

    if result.kind == "failed":
        logger.warning("audit.load.failed sid=%s turn=%s error=%s", sid, turn, result.error)
    elif result.kind == "miss":
        logger.warning("audit.load.miss sid=%s turn=%s", sid, turn)
    elif result.kind == "turn_mismatch":
        logger.warning(
            "audit.load.turn_mismatch sid=%s turn=%s actual=%s",
            sid,
            turn,
            result.actual_turn,
        )
    else:  # timeout
        logger.warning("audit.load.timeout sid=%s turn=%s", sid, turn)

    pg_fb = await _pg_crisis_fallback(ctx)
    return {
        "audit_state": {
            "crisis_locked": pg_fb["crisis_locked"],
            "crisis_detected": False,
            "redline_triggered": False,
            "guidance": None,
            "target_message_id": pg_fb["target_message_id"],
        },
    }


def _all_false_audit_state() -> dict:
    """降级用的 all-False audit_state。"""
    return {
        "audit_state": {
            "crisis_locked": False,
            "crisis_detected": False,
            "redline_triggered": False,
            "guidance": None,
            "target_message_id": None,
        },
    }


def route_by_risk(state: MainDialogueState) -> str:
    """5 signals → 4 routing outputs (baseline §7.1.1).

    Priority: crisis_locked (① sticky) > crisis_detected (②) >
              redline_triggered (③) > guidance (④) > else (⑤ main)

    Args:
        state["audit_state"]: AuditState with keys crisis_locked / crisis_detected /
                              redline_triggered / guidance
    Returns:
        "crisis" | "redline" | "guidance" | "main"
    """
    audit: AuditState = state["audit_state"]
    if audit["crisis_locked"] or audit["crisis_detected"]:
        return "crisis"
    if audit["redline_triggered"]:
        return "redline"
    if audit["guidance"] is not None:
        return "guidance"
    return "main"


# ---------------------------------------------------------------------------
# 装配节点：3 个 build_messages_*（M9 主体 D 层仅改函数体，拓扑零 diff）
# ---------------------------------------------------------------------------


async def build_messages_main(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    """W1 wrapper 模式：load_active_history_for_assembly + 末位 wrapped HumanMessage。

    装配顺序：
      [system_prompt, *summaries(前缀), *active_messages(不含本轮),
       HumanMessage(content=format_guidance_wrapper(ctx.user_input, audit.guidance))]

    职责边界：
    - history 不含本轮 human（由 load_active_history_for_assembly 的 until_turn 过滤）
    - wrapper 仅作用于 LLM 输入装配层，不回写 messages 表
    - compression 路径（me.py 预装 messages）时本节点跳过装配
    - build_context（audit 路径用）保留不删
    """
    ctx = runtime.context

    # compression 路径：me.py 已预装 messages，跳过 DB 重装
    if state.get("messages"):
        return {}

    async with ctx.db_session_factory() as db:
        history = await load_active_history_for_assembly(
            ctx.session_id,
            state["turn_number"],
            db,
        )
    system_prompt = build_system_prompt(ctx.age, ctx.gender)

    audit = state.get("audit_state", {})
    return {
        "messages": [
            system_prompt,
            *history,
            HumanMessage(
                content=format_guidance_wrapper(
                    ctx.user_input,
                    audit.get("guidance"),
                )
            ),
        ]
    }


async def build_messages_crisis(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    """crisis 专属装配：crisis system prompt → anchor_window → after_anchor → reentry wrapper。"""
    ctx = runtime.context
    audit = state.get("audit_state", {})

    target_mid = audit.get("target_message_id")
    assert target_mid is not None, (
        "M9 Step 8 前 target_message_id 由 audit 节点必填；None 表示 PG 兜底或 Redis 信号尚未就绪"
    )

    async with ctx.db_session_factory() as db:
        anchor_system, after_anchor = await build_crisis_context(
            ctx.session_id,
            db,
            target_mid,
        )
    return {
        "messages": [
            build_crisis_system_prompt(ctx.age, ctx.gender),
            anchor_system,
            *after_anchor,
            HumanMessage(content=format_reentry_wrapper_crisis(ctx.user_input)),
        ]
    }


async def build_messages_redline(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    """redline 专属装配：redline system prompt → summaries → recent pairs → reentry wrapper。"""
    ctx = runtime.context

    async with ctx.db_session_factory() as db:
        summaries_systems, recent_pairs = await build_redline_context(
            ctx.session_id,
            state["turn_number"],
            db,
        )
    return {
        "messages": [
            build_redline_system_prompt(ctx.age, ctx.gender),
            *summaries_systems,
            *recent_pairs,
            HumanMessage(content=format_reentry_wrapper_redline(ctx.user_input)),
        ]
    }


# ---------------------------------------------------------------------------
# LLM 节点（Runtime DI，资源从 runtime.context 获取）
# ---------------------------------------------------------------------------


async def _stream_llm_chunks(
    state: MainDialogueState,
    ctx: ChatContextSchema,
    llm: Runnable,
    provider: str,
    intervention_type: str | None,
) -> dict:
    """3 个 call_*_llm 公共 LLM 流式消费 + chunk 信号派发(G3-1 落点)。

    本节点实现内部私有协程(下划线前缀模块内私有),非 Runnable /
    非 @traceable,不产新 LangGraph span(节点名仍为 call_main/crisis/
    redline_llm,LangGraph 节点名取自函数名,G3-5 trace 零变化)。

    公共行为(verbatim 复原原 3 个 call_*_llm 公共部分,G3-4 行为字节级等价):
    - 消息消费 state["messages"](list copy)
    - intervention_type emit(emit 时机:在 async for 之前;None 时跳过)
    - async for chunk llm.astream(llm_messages)
    - 4 段 chunk 派发:reasoning → delta text → finish_reason → usage_metadata
    - return AIMessage 拼接完整内容

    差异由参数注入:llm 工厂 / provider key / intervention_type 字符串。
    """
    writer = get_stream_writer()
    parts: list[str] = []
    llm_messages = list(state["messages"])

    if intervention_type is not None:
        writer({"intervention_type": intervention_type})

    async for chunk in llm.astream(llm_messages):
        # astream() yields AIMessageChunk at runtime despite BaseMessage type annotation
        _chunk_typed: AIMessageChunk = chunk  # type: ignore[assignment]

        # reasoning passthrough (signal only, no text, baseline §3.2)
        if extract_reasoning_content(_chunk_typed, provider):
            writer({"reasoning": True})

        text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
        if text:
            writer({"delta": text})
            parts.append(text)

        # finish_reason passthrough (whitelist only, helper dispatch)
        fr = extract_finish_reason(_chunk_typed, provider)
        if fr:
            writer({"finish_reason": fr})

        # usage_metadata passthrough：末帧 usage-only chunk 由 SDK 自动注入
        if _chunk_typed.usage_metadata is not None:
            usage = extract_usage(_chunk_typed)
            if usage:
                writer({"usage_metadata": usage})

    return {"messages": [AIMessage(content="".join(parts))]}


async def call_main_llm(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    """调主对话 LLM，通过 get_stream_writer() 流式输出 chunk。

    委托 `_stream_llm_chunks` 公共协程(本节点内私有 helper,非 Runnable)。

    消息已由 build_messages_* 节点装配到 state["messages"] 中，
    本节点直接消费（不再调历史装配模式）。

    LLM 通过 Runtime DI 的 settings 构造（替代 M8 期 lru_cache 工厂），
    参数与 build_main_llm(settings) 完全一致。

    finish_reason passthrough: only white-list values
    (stop / length / content_filter) are forwarded; others fall through
    to the caller which emits "stop" as the default.

    No DB writes: persist_ai_turn is called from me.py generator after
    the stream ends (T5 single-write-point = generator).
    """
    ctx = runtime.context
    guidance = state.get("audit_state", {}).get("guidance")
    return await _stream_llm_chunks(
        state,
        ctx,
        llm=build_main_llm(ctx.settings),
        provider=ctx.settings.main_provider,
        intervention_type="guided" if guidance is not None else None,
    )


async def call_crisis_llm(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    """调 crisis 干预 LLM，通过 get_stream_writer() 流式输出 chunk。

    委托 `_stream_llm_chunks` 公共协程。干预类型无条件 emit `"crisis"`。
    """
    ctx = runtime.context
    return await _stream_llm_chunks(
        state,
        ctx,
        llm=build_crisis_llm(ctx.settings),
        provider=ctx.settings.audit_provider,
        intervention_type="crisis",
    )


async def call_redline_llm(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    """调 redline 干预 LLM，通过 get_stream_writer() 流式输出 chunk。

    委托 `_stream_llm_chunks` 公共协程。干预类型无条件 emit `"redline"`。
    """
    ctx = runtime.context
    return await _stream_llm_chunks(
        state,
        ctx,
        llm=build_redline_llm(ctx.settings),
        provider=ctx.settings.audit_provider,
        intervention_type="redline",
    )


# ---------------------------------------------------------------------------
# 图工厂（替换模块级 _builder + main_graph 单例）
# ---------------------------------------------------------------------------


def build_main_graph() -> CompiledStateGraph:
    """构建主对话图（7 节点 + 4 分支条件路由）。"""
    builder = StateGraph(MainDialogueState, context_schema=ChatContextSchema)

    builder.add_node("load_audit_state", load_audit_state)
    builder.add_node("build_messages_main", build_messages_main)
    builder.add_node("build_messages_crisis", build_messages_crisis)
    builder.add_node("build_messages_redline", build_messages_redline)
    builder.add_node("call_main_llm", call_main_llm)
    builder.add_node("call_crisis_llm", call_crisis_llm)
    builder.add_node("call_redline_llm", call_redline_llm)

    builder.set_entry_point("load_audit_state")

    builder.add_conditional_edges(
        "load_audit_state",
        route_by_risk,
        {
            "crisis": "build_messages_crisis",
            "redline": "build_messages_redline",
            "guidance": "build_messages_main",
            "main": "build_messages_main",
        },
    )

    builder.add_edge("build_messages_main", "call_main_llm")
    builder.add_edge("build_messages_crisis", "call_crisis_llm")
    builder.add_edge("build_messages_redline", "call_redline_llm")
    builder.add_edge("call_main_llm", END)
    builder.add_edge("call_crisis_llm", END)
    builder.add_edge("call_redline_llm", END)

    return builder.compile()  # type: ignore[reportReturnType]
