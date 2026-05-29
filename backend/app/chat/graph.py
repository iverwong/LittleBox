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
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
)
from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.redis_client import _build_arq_redis_url
from app.chat.context import build_context, load_active_history_for_assembly
from app.chat.context_schema import ChatContextSchema
from app.chat.extractors import extract_finish_reason, extract_reasoning_content, extract_usage
from app.chat.factory import build_main_llm
from app.chat.prompts import build_system_prompt, format_guidance_wrapper
from app.chat.state import AuditState, MainDialogueState
from app.config import settings
from app.models.chat import Message, Session
from app.models.enums import InterventionType, MessageRole, MessageStatus
from app.state.audit_signals import AuditSignalsManager

if TYPE_CHECKING:
    from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: persistence + audit (called from me.py generator, NOT from graph)
# ---------------------------------------------------------------------------


async def persist_ai_turn(
    db: AsyncSession,
    sid: uuid.UUID,
    finish_reason: str,
    content: str,
    intervention_type: InterventionType | None = None,
) -> uuid.UUID:
    """Persist one AI turn as an active message row (M6-patch3: no longer updates last_active_at).

    T5 single-write-point: called from me.py generator after the stream ends.
    This helper does NOT touch the messages table inside the graph.
    last_active_at 由 commit① 独占（F 决策），commit② 不再覆写。

    Args:
        db: async DB session
        sid: session UUID
        finish_reason: LLM stop reason (stop / length / content_filter / user_stopped)
        content: accumulated text content
        intervention_type: None=normal, crisis=redline=guided=override type

    Returns:
        The id of the newly inserted AI message row (uuid.UUID).
    """
    msg = Message(
        session_id=sid,
        role=MessageRole.ai,
        content=content,
        status=MessageStatus.active,
        finish_reason=finish_reason,
        intervention_type=intervention_type,
    )
    db.add(msg)
    await db.flush()  # populate msg.id
    # M8: ai_turn_counter 同事务 +1（SQL 列表达式，PG 行锁安全）
    await db.execute(
        update(Session)
        .where(Session.id == sid)
        .values(ai_turn_counter=Session.ai_turn_counter + 1)
    )
    return msg.id


async def enqueue_audit(
    sid: uuid.UUID,
    db: AsyncSession,
    turn_number: int,
    child_user_id: uuid.UUID,
    target_message_id: uuid.UUID,
) -> None:
    """SET Redis pending + ARQ enqueue 触发异步审查。

    D4 决议：先 SET pending 再 enqueue 再返回。~~5-10ms 延迟换确定性。
    资源管理：每次调用建立独立 ArqRedis 连接，用后关闭（M14 优化为单例）。
    T10（D-patch0-7）：child_user_id 参数由 me.py 从 ChatContextSchema 取值下传，
    供 ARQ worker 构造 AuditContextSchema 使用，避免 worker 内 SELECT 反查。
    """
    from arq import create_pool
    from arq.connections import RedisSettings
    from redis.asyncio import Redis

    # T13（γ-2）：_build_arq_redis_url 是单一来源 helper，
    # auth/redis_client.py 中定义，runtime.py:45 共享调用。
    _audit_redis_url = _build_arq_redis_url()
    redis = Redis.from_url(_audit_redis_url, encoding="utf-8", decode_responses=True)

    # 提取 ARQ 连接参数（需在 redis.aclose() 之前，否则 connection_pool 变为 None）
    _arq_host = str(redis.connection_pool.connection_kwargs.get("host", "localhost"))
    _arq_port = int(redis.connection_pool.connection_kwargs.get("port", 6379))
    _arq_password = redis.connection_pool.connection_kwargs.get("password")

    try:
        manager = AuditSignalsManager(redis, ttl=settings.audit_redis_ttl_seconds)
        await manager.set_pending(str(sid), turn_number, started_at=datetime.now(UTC).isoformat())
    finally:
        await redis.aclose()

    # 2) ARQ enqueue
    arq_pool = await create_pool(
        RedisSettings(
            host=_arq_host,
            port=_arq_port,
            password=_arq_password,
            database=settings.arq_redis_db,
        ),
    )
    try:
        await arq_pool.enqueue_job(
            "run_audit", str(sid), turn_number, str(child_user_id), str(target_message_id),
            _job_id=f"audit:{sid}:{turn_number}",
        )
    finally:
        await arq_pool.close()
        await arq_pool.connection_pool.disconnect()  # type: ignore[attr-defined] — arq stubs

    logger.info("audit.enqueued sid=%s turn=%s", sid, turn_number)


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------


async def load_audit_state(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    """加载审查信号（Redis poll_wait 协议 + 首轮快速通道）。

    首轮（turn_number == 1）：直接返回 all-False，不进等待。
    非首轮：poll_wait(sid, expected_turn=turn_number-1)。6 分支：
      ready  → 注入信号
      failed → 全 False + 日志
      miss   → 全 False + 日志
      turn_mismatch → 全 False + 日志
      timeout → 全 False + 日志
    （pending 在 poll_wait 内循环等待直到 ready 或超时，不作为一个分支返回）

    crisis_locked 的 PG 查询留 M9（本 M8 期不读）。
    """
    ctx = runtime.context
    turn = state.get("turn_number", 1)
    sid = str(ctx.session_id)

    if turn == 1:
        # 首轮：不进等待
        return _all_false_audit_state()

    manager = AuditSignalsManager(
        ctx.audit_redis, ttl=ctx.settings.audit_redis_ttl_seconds,
    )
    result = await manager.poll_wait(
        sid, expected_turn=turn - 1,
        timeout=ctx.settings.audit_wait_timeout_seconds,
    )

    if result.kind == "ready" and result.signals is not None:
        logger.info("audit.load.ready sid=%s turn=%s", sid, turn)
        return {
            "audit_state": {
                "crisis_locked": False,
                "crisis_detected": result.signals.crisis_detected,
                "redline_triggered": result.signals.redline_triggered,
                "guidance": result.signals.guidance,
                "target_message_id": None,  # M9 Step 8 信号协议扩展后从 signals 读取
            },
        }

    if result.kind == "failed":
        logger.warning("audit.load.failed sid=%s turn=%s error=%s", sid, turn, result.error)
    elif result.kind == "miss":
        logger.warning("audit.load.miss sid=%s turn=%s", sid, turn)
    elif result.kind == "turn_mismatch":
        logger.warning(
            "audit.load.turn_mismatch sid=%s turn=%s actual=%s",
            sid, turn, result.actual_turn,
        )
    else:  # timeout
        logger.warning("audit.load.timeout sid=%s turn=%s", sid, turn)

    return _all_false_audit_state()


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
    """W1 wrapper 模式：load_active_history_for_assembly（不含本轮 human）+ 末位 wrapped HumanMessage。

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
            ctx.session_id, state["turn_number"], db,
        )
    system_prompt = build_system_prompt(ctx.age, ctx.gender)

    audit = state.get("audit_state", {})
    return {"messages": [
        system_prompt,
        *history,
        HumanMessage(content=format_guidance_wrapper(
            ctx.user_input, audit.get("guidance"),
        )),
    ]}


async def build_messages_crisis(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    """patch0 期委派 build_messages_main；M9 主体 D 层替换为 crisis 专属装配。"""
    return await build_messages_main(state, runtime)


async def build_messages_redline(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    """patch0 期委派 build_messages_main；M9 主体 D 层替换为 redline 专属装配。"""
    return await build_messages_main(state, runtime)


# ---------------------------------------------------------------------------
# LLM 节点（Runtime DI，资源从 runtime.context 获取）
# ---------------------------------------------------------------------------


async def call_main_llm(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    """调主对话 LLM，通过 get_stream_writer() 流式输出 chunk。

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
    writer = get_stream_writer()
    llm = build_main_llm(ctx.settings)
    parts: list[str] = []

    # 消息已由 build_messages_* 节点装配，直接读 state["messages"]
    llm_messages = list(state["messages"])

    provider = ctx.settings.main_provider

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


async def call_crisis_llm(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    """Crisis LLM stub — patch0 回退到主 LLM。

    M9 主体 D 层替换为危机干预专属 prompt + 模型。函数体零拓扑 diff。
    """
    logger.warning("crisis_llm not implemented, falling back to main_llm in patch0")
    return await call_main_llm(state, runtime)


async def call_redline_llm(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    """Redline LLM stub — patch0 回退到主 LLM。

    M9 主体 D 层替换为红线干预专属 prompt + 模型。函数体零拓扑 diff。
    """
    logger.warning("redline_llm not implemented, falling back to main_llm in patch0")
    return await call_main_llm(state, runtime)


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

    return builder.compile()
