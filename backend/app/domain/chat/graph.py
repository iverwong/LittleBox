"""主对话 LangGraph — 5 节点 + 1 条件路由（2 分支）。

图拓扑（redline 移除后，guidance 注入）：
    START → load_audit_state → route_by_risk
    ├─ crisis → build_messages_crisis → call_crisis_llm → END
    └─ main   → build_messages_main   → call_main_llm    → END

redline 和 guidance 注入在同一层级处理：两者都在
load_audit_state 中合并到 guidance 字段，通过 format_guidance_wrapper
注入下一轮 human 消息，不再走独立 LLM 节点。

周知（T5）：
  - persist_ai_turn / enqueue_audit 为顶层 helper（me.py generator 调用），不在图内
  - me.py generator 是单写入点（T5 single-write-point）
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
)
from langchain_core.runnables import Runnable
from langgraph.config import get_stream_writer
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import select, update

from app.core.enums import InterventionType, MessageRole, MessageStatus
from app.core.history_xml import serialize_history_to_xml
from app.core.llm import build_compression_llm, build_crisis_llm, build_main_llm
from app.core.llm_extractors import (
    extract_finish_reason,
    extract_reasoning_content,
    extract_usage,
    role_profile,
)
from app.core.llm_topology import ModelProfile, Role
from app.core.models import AuditRecord, Session
from app.domain.audit.models import RollingSummary
from app.domain.audit.signals import AuditSignalsManager
from app.domain.chat.compression import (
    build_compression_messages,
    extract_compression_summary,
    split_for_compression,
)
from app.domain.chat.context import (
    load_active_messages_with_summary,
    load_recent_messages,
    to_lc_message,
)
from app.domain.chat.context_schema import ChatContextSchema
from app.domain.chat.models import Message
from app.domain.chat.prompts import (
    build_crisis_system_prompt,
    build_system_prompt,
    format_guidance_wrapper,
)
from app.domain.chat.state import AuditState, MainDialogueState

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

    红线处理（redline_triggered）：审查 agent 通过 guidance_injection 字段
    注入提醒，不再走独立 redline LLM 节点。

    PG 查询 _pg_crisis_fallback 在 ready 和降级分支都执行，(b) 单一来源。"""
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
            "guidance": None,
            "target_message_id": None,
        },
    }


def route_by_risk(state: MainDialogueState) -> str:
    """路由：crisis_locked → "crisis"，否则 → "main"。

    guidance 注入不再走独立图分支。redline 和 guidance 都在
    load_audit_state 中合并到 guidance 字段，由 build_messages_main
    的 format_guidance_wrapper 注入。

    Returns:
        "crisis" | "main"
    """
    audit: AuditState = state["audit_state"]
    if audit["crisis_locked"]:
        return "crisis"
    return "main"


# ---------------------------------------------------------------------------
# 装配节点：2 个 build_messages_*（M9 主体 D 层仅改函数体，拓扑零 diff）
# ---------------------------------------------------------------------------


async def build_messages_main(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    """W1 wrapper 模式:图内压缩 + system_prompt + history + wrapped HumanMessage。

    注意：pipeline.py 可能在 pre-graph 阶段已执行阻塞压缩并清
    needs_compression=False 落库。本节点重查 DB 决定是否进图内压缩，
    两者互为保护路径（pre-graph 成功则本节点不重复压缩）。

    装配顺序：
      [system_prompt(含 # 历史会话摘要(压缩) 段), *history(不含本轮),
       HumanMessage(content=format_guidance_wrapper(ctx.user_input, audit.guidance))]

    压缩逻辑与 crisis 路径复用相同 _handle_compress 函数。
    发射 compression_start/end payload，由 pipeline.py 图循环转发为 SSE 帧。

    职责边界：
    - history 不含本轮 human(to_turn 边界过滤),不含 summary 行
    - summary 由 load_active_messages_with_summary 拆分注入 build_system_prompt
    - wrapper 仅作用于 LLM 输入装配层,不回写 messages 表
    """
    ctx = runtime.context
    sid = ctx.session_id
    audit = state.get("audit_state", {})
    guidance = audit.get("guidance")

    async with ctx.db_session_factory() as db:
        needs_compression = await db.scalar(
            select(Session.needs_compression).where(Session.id == sid)
        )

        history_rows, summary_orm = await load_active_messages_with_summary(
            ctx.session_id, db, to_turn=state["turn_number"] - 1
        )

        if needs_compression:
            to_compress_orm, to_keep_orm = split_for_compression(history_rows)
            writer = get_stream_writer()
            writer({"compression_start": {}})

            to_compress = [to_lc_message(mo) for mo in to_compress_orm]
            to_compress_ids = [mo.id for mo in to_compress_orm]
            old_summary = to_lc_message(summary_orm) if summary_orm else None

            new_summary = await _handle_compress(
                ctx, sid, old_summary, to_compress, to_compress_ids
            )

            history = [to_lc_message(mo) for mo in to_keep_orm]
            summary_content = new_summary

            writer({"compression_end": {}})
        else:
            summary_content = summary_orm.content if summary_orm else None
            history = [to_lc_message(mo) for mo in history_rows]

    system_prompt = build_system_prompt(ctx.child_profile, summary_content)

    return {
        "messages": [
            system_prompt,
            *history,
            HumanMessage(content=format_guidance_wrapper(ctx.user_input, guidance)),
        ]
    }


async def _handle_compress(
    ctx: ChatContextSchema,
    sid: UUID,
    summary: BaseMessage | None,
    to_compress: list[BaseMessage],
    to_compress_ids: list[UUID],
) -> str:
    c_input = build_compression_messages(
        summary,
        to_compress,
    )
    c_llm = build_compression_llm(ctx.settings)
    c_result = await c_llm.ainvoke(c_input)
    raw = c_result.content if hasattr(c_result, "content") else str(c_result)
    new_summary = extract_compression_summary(raw)
    _summary_obj = Message(
        session_id=sid,
        role=MessageRole.summary,
        status=MessageStatus.active,
        content=new_summary,
    )
    async with ctx.db_session_factory() as db:
        await db.execute(
            update(Message)
            .where(Message.id.in_(to_compress_ids))
            .values(status=MessageStatus.compressed)
        )
        await db.execute(update(Session).where(Session.id == sid).values(needs_compression=False))
        db.add(_summary_obj)
        await db.commit()

    return new_summary


async def _build_crisis_context(
    ctx: ChatContextSchema,
    sid: UUID,
    crisis_message_turn: int,
    current_turn: int,
    guidance: str | None,
    summary: str | None = None,
    keep_messages: list[BaseMessage] = [],
) -> list[BaseMessage]:
    """构建风险态上下文

    1. 获取审查agent输出的风险主题
    2. 获取N-3到N+3轮次的消息
    3. 按风险轮次前后进行拆分

    Args:
        ctx (ChatContextSchema): langchain 图上下文
        db (AsyncSession): 数据库连接
        sid (UUID): session id
        crisis_message_turn (int): 风险触发轮次
        current_turn (int): 当前对话轮次
        guidance (str | None): audit 注入内容
        summary (str | None, optional): 压缩摘要. Defaults to None.
        keep_messages (list[BaseMessage], optional): 保留的对话，直接传入messages. Defaults to [].

    Raises:
        ValueError: crisis_topic 与 crisis_locked 表示的语义不一致时触发，crisis_locked 为 True 时，
        crisis_topic 应该不为 None

    Returns:
        list[BaseMessage]: 返回[sys, human, ai, ... , human]结构的messages
    """
    async with ctx.db_session_factory() as db:
        # 查crisis_topic
        crisis_topic = await db.scalar(
            select(AuditRecord.crisis_topic).where(
                AuditRecord.session_id == sid,
                AuditRecord.turn_number == crisis_message_turn,
            )
        )
        if crisis_topic is None:
            raise ValueError("crisis_topic 为空，但 audit 状态 crisis_locked 查询为 True")
        # 查messages，不包括最新 human
        messages_orm = await load_recent_messages(
            sid,
            db,
            crisis_message_turn - 3,
            min(crisis_message_turn + 3, current_turn - 1),
            as_orm=True,
        )
        # 分隔
        pre_messages: list[BaseMessage] = []
        crisis_messages: list[BaseMessage] = []
        post_messages: list[BaseMessage] = []
        for mo in messages_orm:
            if mo.turn_number < crisis_message_turn:
                pre_messages.append(to_lc_message(mo))
            elif mo.turn_number == crisis_message_turn:
                crisis_messages.append(to_lc_message(mo))
            else:
                post_messages.append(to_lc_message(mo))

    return [
        build_crisis_system_prompt(
            ctx.child_profile,
            crisis_topic,
            serialize_history_to_xml(crisis_messages),
            serialize_history_to_xml(pre_messages),
            serialize_history_to_xml(post_messages),
            summary,
        ),
        *keep_messages,
        HumanMessage(content=format_guidance_wrapper(ctx.user_input, guidance)),
    ]


async def build_messages_crisis(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    """根据会话情况构建风险 agent 所需的 messages

    1. 获取会话的压缩标志和风险触发轮次
    2. 检验风险触发轮次不为空
    3. 根据压缩态拼装最终 messages
    """
    ctx = runtime.context
    sid = ctx.session_id
    audit = state.get("audit_state", {})
    guidance = audit.get("guidance")
    current_turn = state["turn_number"]

    async with ctx.db_session_factory() as db:
        needs_compression = await db.scalar(
            select(Session.needs_compression).where(Session.id == sid)
        )
        crisis_message_turn = await db.scalar(
            select(Message.turn_number)
            .join(RollingSummary, RollingSummary.crisis_locked_message_id == Message.id)
            .where(RollingSummary.session_id == sid)
        )
        if crisis_message_turn is None:
            raise ValueError(
                "crisis_locked_message_id 为空，但 audit 状态 crisis_locked 查询为 True"
            )

        messages_orm, summary_orm = await load_active_messages_with_summary(
            sid, db, from_turn=crisis_message_turn + 4, to_turn=current_turn - 1
        )
        keep_messages = [to_lc_message(mo) for mo in messages_orm]
        summary_content = summary_orm.content if summary_orm else None

        # 路由标记：连接内决策 + DB 写操作，连接外只走纯值数据流。
        to_compress: list[BaseMessage] = []
        to_compress_ids: list[UUID] = []
        to_keep: list[BaseMessage] = []
        old_summary: BaseMessage | None = None
        if needs_compression and current_turn - crisis_message_turn > 3:
            to_compress_orm, to_keep_orm = split_for_compression(messages_orm)
            to_compress = [to_lc_message(mo) for mo in to_compress_orm]
            to_compress_ids = [mo.id for mo in to_compress_orm]
            to_keep = [to_lc_message(mo) for mo in to_keep_orm]
            old_summary = to_lc_message(summary_orm) if summary_orm else None
            action = "compress"
        elif needs_compression:
            # crisis 触发后 3 轮内不压缩，但必须清零标志避免下轮误进。
            await db.execute(
                update(Session).where(Session.id == sid).values(needs_compression=False)
            )
            await db.commit()
            action = "keep"
        else:
            action = "keep"

    if action == "compress":
        writer = get_stream_writer()
        writer({"compression_start": {}})
        new_summary = await _handle_compress(ctx, sid, old_summary, to_compress, to_compress_ids)
        writer({"compression_end": {}})
        return {
            "messages": await _build_crisis_context(
                ctx,
                sid,
                crisis_message_turn,
                current_turn,
                guidance,
                new_summary,
                to_keep,
            )
        }

    return {
        "messages": await _build_crisis_context(
            ctx,
            sid,
            crisis_message_turn,
            current_turn,
            guidance,
            summary_content,
            keep_messages,
        )
    }


# ---------------------------------------------------------------------------
# LLM 节点（Runtime DI，资源从 runtime.context 获取）
# ---------------------------------------------------------------------------


async def _stream_llm_chunks(
    state: MainDialogueState,
    ctx: ChatContextSchema,
    llm: Runnable,
    profile: ModelProfile,
    intervention_type: str | None,
) -> dict:
    """2 个 call_*_llm 公共 LLM 流式消费 + chunk 信号派发(G3-1 落点)。

    本节点实现内部私有协程(下划线前缀模块内私有),非 Runnable /
    非 @traceable,不产新 LangGraph span(节点名仍为 call_main/crisis_llm,
    LangGraph 节点名取自函数名,G3-5 trace 零变化)。

    公共行为(verbatim 复原原 2 个 call_*_llm 公共部分,G3-4 行为字节级等价):
    - 消息消费 state["messages"](list copy)
    - intervention_type emit(emit 时机:在 async for 之前;None 时跳过)
    - async for chunk llm.astream(llm_messages)
    - 4 段 chunk 派发:reasoning → delta text → finish_reason → usage_metadata
    - return AIMessage 拼接完整内容

    差异由参数注入:llm 工厂 / 模型档(决定 reasoning 提取路径)/
    intervention_type 字符串。
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
        if extract_reasoning_content(_chunk_typed, profile):
            writer({"reasoning": True})

        text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
        if text:
            writer({"delta": text})
            parts.append(text)

        # finish_reason passthrough (whitelist only, helper dispatch)
        fr = extract_finish_reason(_chunk_typed)
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
        # 字节等价于旧 main 端点默认值 "deepseek"(关注点 1 要求零兜底依赖):
        # 由 Role.MAIN 解析模型档,与原 provider 字符串路径同语义。
        profile=role_profile(Role.MAIN),
        intervention_type=InterventionType.guided if guidance is not None else None,
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
        # crisis 复用 main 绑定(Step 3 收口),模型档同步沿用 main。
        profile=role_profile(Role.MAIN),
        intervention_type=InterventionType.crisis,
    )


# ---------------------------------------------------------------------------
# 图工厂（替换模块级 _builder + main_graph 单例）
# ---------------------------------------------------------------------------


def build_main_graph() -> CompiledStateGraph:
    """构建主对话图（5 节点 + 2 分支条件路由）。"""
    builder = StateGraph(MainDialogueState, context_schema=ChatContextSchema)

    builder.add_node("load_audit_state", load_audit_state)
    builder.add_node("build_messages_main", build_messages_main)
    builder.add_node("build_messages_crisis", build_messages_crisis)
    builder.add_node("call_main_llm", call_main_llm)
    builder.add_node("call_crisis_llm", call_crisis_llm)

    builder.add_edge("__start__", "load_audit_state")

    builder.add_conditional_edges(
        "load_audit_state",
        route_by_risk,
        {
            "crisis": "build_messages_crisis",
            "main": "build_messages_main",
        },
    )

    builder.add_edge("build_messages_main", "call_main_llm")
    builder.add_edge("build_messages_crisis", "call_crisis_llm")
    builder.add_edge("call_main_llm", "__end__")
    builder.add_edge("call_crisis_llm", "__end__")

    return builder.compile()  # type: ignore[reportReturnType]
