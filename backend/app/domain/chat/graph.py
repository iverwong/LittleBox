"""主对话 LangGraph 状态机定义 — 5 节点 + 1 条件路由(2 分支)。

图拓扑::

    START → load_audit_state → route_by_risk
    ├─ crisis → build_messages_crisis → call_crisis_llm → END
    └─ main   → build_messages_main   → call_main_llm    → END

guidance 注入由 load_audit_state 节点合并到 ``audit_state.guidance``,
再由 main 路径的 ``build_messages_main`` 通过 ``format_guidance_wrapper``
注入到本轮 HumanMessage 的 content,不再走独立 LLM 节点。

模块外职责边界:
- ``persist_ai_turn`` / ``enqueue_audit`` 为顶层 helper,不在图内
- ``api/me.py::chat_stream`` 的流式 generator 是 ai 消息落库 / 审查入队的
  唯一写入点
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
# 审查信号加载节点 + 风险路由
# ---------------------------------------------------------------------------


async def _pg_crisis_fallback(ctx) -> dict:
    """从 PG ``rolling_summaries.crisis_locked_message_id`` 读危机锁定状态。

    内部开闭 db session,仅查当前 session 对应 RollingSummary 单行。
    rolling_summaries 是 sticky lock 的单一真相源——一旦写入,危机锁定
    状态在 PG 与 Redis 双源中以 PG 为准。

    Returns:
        包含 ``crisis_locked``(是否锁定)与 ``target_message_id``
        (锁定指向的 ai 消息 id;未锁定时为 None)的 dict。
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
    """加载本轮审查信号,填充 ``audit_state``。

    数据源策略:Redis 主源 + PG 粘性锁兜底。
    Redis 仅承载当轮信号(``crisis_detected`` / ``guidance``);sticky
    ``crisis_locked`` 必须以 PG ``rolling_summaries`` 为准,因为 Redis
    信号 TTL 到期后不能再让危机路径失锁。

    分支拓扑:
        turn == 1                 → 早退 all-False(首轮无审查信号可读)
        ready                     → Redis 当轮信号 + PG 粘性 crisis_locked
        failed / miss / turn_mismatch / timeout → PG 粘性 crisis_locked
                                                  + 当轮 all-False + 日志

    PG 查询 ``_pg_crisis_fallback`` 在 ready 与降级分支都执行,
    保证 crisis_locked 在两条路径下语义一致。

    Args:
        state: 当前轮 MainDialogueState,使用 ``turn_number``。
        runtime: LangGraph Runtime,提供 ChatContextSchema(含 audit_redis
            与 settings 中的 ttl / timeout)。

    Returns:
        ``audit_state`` 字段的更新 dict,结构与 ``AuditState`` TypedDict
        一致。
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
    """按 ``crisis_locked`` 路由到 crisis / main 分支。

    guidance 注入不参与路由决策——guidance 与红线提示都在
    ``load_audit_state`` 中合并到 ``audit_state.guidance`` 字段,由
    ``build_messages_main`` 通过 ``format_guidance_wrapper`` 注入到
    HumanMessage.content,仅作用于 LLM 输入装配层。

    Args:
        state: 当前 MainDialogueState,读 ``audit_state.crisis_locked``。

    Returns:
        ``"crisis"`` 或 ``"main"``,对应 LangGraph 条件路由的下一个节点。
    """
    audit: AuditState = state["audit_state"]
    if audit["crisis_locked"]:
        return "crisis"
    return "main"


# ---------------------------------------------------------------------------
# 消息装配节点:build_messages_main 与 build_messages_crisis 分别装配两条
# 路径的 LLM 输入 messages,共享 _handle_compress 压缩子流程
# ---------------------------------------------------------------------------


async def build_messages_main(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    """装配 main 路径的 LLM 输入 messages。

    装配顺序::

        [system_prompt(含 # 历史会话摘要(压缩) 段),
         *history(不含本轮,不含 summary 行),
         HumanMessage(content=format_guidance_wrapper(ctx.user_input,
                                                     audit.guidance))]

    压缩决策:本节点重查 ``Session.needs_compression`` 决定是否进图内
    压缩子流程。``pipeline.py`` 可能在 pre-graph 阶段已经执行阻塞压缩
    并清零 needs_compression 落库;本节点重查是双保险,两者互为保护路径,
    pre-graph 成功则本节点跳过压缩,反之亦然。压缩子流程与 crisis 路径
    复用同一份 ``_handle_compress``。

    职责边界:
    - ``history`` 不含本轮 human(to_turn 边界过滤),不含 summary 行
    - summary 由 ``load_active_messages_with_summary`` 拆分注入
      ``build_system_prompt``
    - wrapper 仅作用于 LLM 输入装配层,不会回写 messages 表

    图内压缩通过 SSE 信号 ``compression_start`` / ``compression_end``
    派发起止帧,由 ``pipeline.py`` 的图循环转发为 SSE 事件。

    Args:
        state: 当前 MainDialogueState,读 ``turn_number`` 与 ``audit_state``。
        runtime: LangGraph Runtime,提供 ChatContextSchema。

    Returns:
        ``messages`` 字段的更新 dict,内容为装配后的 LLM 输入。
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
    """执行一次图内压缩子流程并落库。

    流程:
        1. 调 ``build_compression_llm`` 跑压缩,得到新摘要文本
        2. 新增 summary 行(``MessageRole.summary``)
        3. 将待压缩消息标记为 ``MessageStatus.compressed``
        4. 清零 ``Session.needs_compression``

    Args:
        ctx: ChatContextSchema,用于取 settings 与 db_session_factory。
        sid: 当前 session id。
        summary: 上一版压缩摘要对应的 LangChain Message,无摘要时 None。
        to_compress: 待压缩的 LangChain Message 列表。
        to_compress_ids: 与 ``to_compress`` 一一对应的 DB Message.id 列表。

    Returns:
        新摘要的纯文本内容。
    """
    c_input = build_compression_messages(
        summary,
        to_compress,
    )
    c_llm = build_compression_llm(ctx.settings, http_async_client=ctx.shared_http_client)
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
    """构建 crisis 路径的 LLM 输入 messages。

    装配要点:
        1. 查 ``AuditRecord.crisis_topic`` 取审查 agent 输出的风险主题
        2. 加载 ``[crisis_message_turn-3, min(crisis_message_turn+3, current_turn-1)]``
           区间的历史消息,按危机轮次前后拆为 pre / crisis / post 三段
        3. 用 ``serialize_history_to_xml`` 把三段分别 XML 化后注入
           ``build_crisis_system_prompt``
        4. 末尾追加 ``keep_messages`` 与 ``HumanMessage(content=format_guidance_wrapper(...))``

    Args:
        ctx: ChatContextSchema。
        sid: 当前 session id。
        crisis_message_turn: 风险触发轮次(由 RollingSummary.crisis_locked_message_id 解析)。
        current_turn: 当前对话轮次。
        guidance: audit 注入内容(红线详情 / 引导文本)。
        summary: 压缩摘要文本;无摘要时 None。
        keep_messages: 压缩后保留的最近消息列表,直接拼入 messages。

    Raises:
        ValueError: 当 ``crisis_locked`` 为 True 但 ``AuditRecord.crisis_topic``
            为空(语义不一致)。

    Returns:
        ``[system_prompt, *keep_messages, HumanMessage(...)]`` 结构的
        messages 列表。
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
    """装配 crisis 路径的 LLM 输入 messages。

    流程:
        1. 读 ``Session.needs_compression`` 与由
           ``RollingSummary.crisis_locked_message_id`` 解析出的
           ``crisis_message_turn``
        2. 校验 ``crisis_message_turn`` 不为空——为空则抛 ValueError,
           因为 ``audit_state.crisis_locked`` 与 DB 状态语义不一致
        3. 按压缩态分两路:
           - 需要压缩且距危机触发已超 3 轮 → 跑压缩子流程,新摘要进
             ``_build_crisis_context``
           - 其余情况(包含 crisis 触发后 3 轮内不压缩但必须清零
             ``needs_compression`` 避免下轮误进)→ 直接用现有摘要

    DB 块内做读 + 写操作;块退出后再触发可能耗时的 LLM 调用。

    Args:
        state: 当前 MainDialogueState,读 ``turn_number`` 与 ``audit_state``。
        runtime: LangGraph Runtime,提供 ChatContextSchema。

    Returns:
        ``messages`` 字段的更新 dict,内容为 ``_build_crisis_context``
        装配结果。

    Raises:
        ValueError: ``crisis_message_turn`` 解析为空,但 audit_state 标记
            crisis_locked 为 True(语义不一致)。
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

        # DB 块内做读 + 必要的写;后续 LLM 调用挪到块外。
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
            # crisis 触发后 3 轮内不压缩,但清零 needs_compression 避免下轮误进。
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
# LLM 节点:从 runtime.context 取 settings / profile,通过 _stream_llm_chunks
# 公共协程流式消费并派发 SSE 信号
# ---------------------------------------------------------------------------


async def _stream_llm_chunks(
    state: MainDialogueState,
    ctx: ChatContextSchema,
    llm: Runnable,
    profile: ModelProfile,
    intervention_type: str | None,
) -> dict:
    """``call_main_llm`` 与 ``call_crisis_llm`` 共用的流式消费 + 信号派发协程。

    行为要点:
        - 消费 ``state["messages"]``(list copy,避免原地修改影响 reducer)
        - 在 ``async for`` 之前发射 ``intervention_type`` 信号
          (``None`` 时跳过)
        - ``async for chunk in llm.astream(llm_messages)`` 流式拉取
        - 4 段 chunk 派发,顺序为 reasoning → delta text → finish_reason
          → usage_metadata
        - 拼接 ``AIMessage(content)`` 作为节点返回值,供图 state 追加

    节点名仍为 ``call_main_llm`` / ``call_crisis_llm``——LangGraph 节点名
    取自函数名,本内部协程下划线前缀仅在模块内可见,不产新 LangGraph span。

    差异由参数注入:llm 工厂 / 模型档(决定 reasoning 提取路径)
    / ``intervention_type`` 字符串。

    Args:
        state: 当前 MainDialogueState,读 ``messages``。
        ctx: ChatContextSchema(本协程实际未直接使用,保留以备扩展)。
        llm: ``build_main_llm`` / ``build_crisis_llm`` 返回的 Runnable。
        profile: 由 ``role_profile(Role.MAIN)`` 解析的模型档,
            决定 reasoning 字段提取路径。
        intervention_type: ``InterventionType.guided`` / ``.crisis`` 字符串;
            ``None`` 时跳过对应信号。

    Returns:
        ``messages`` 字段更新 dict,内容为拼接后的 ``AIMessage``。
    """
    writer = get_stream_writer()
    parts: list[str] = []
    llm_messages = list(state["messages"])

    if intervention_type is not None:
        writer({"intervention_type": intervention_type})

    async for chunk in llm.astream(llm_messages):
        # astream() yields AIMessageChunk at runtime despite BaseMessage type annotation
        _chunk_typed: AIMessageChunk = chunk  # type: ignore[assignment]

        # reasoning passthrough:仅发信号,不输出文本
        if extract_reasoning_content(_chunk_typed, profile):
            writer({"reasoning": True})

        text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
        if text:
            writer({"delta": text})
            parts.append(text)

        # finish_reason passthrough:仅白名单值(stop / length / content_filter)
        # 通过 helper 分发,其余由调用方默认 emit "stop"
        fr = extract_finish_reason(_chunk_typed)
        if fr:
            writer({"finish_reason": fr})

        # usage_metadata passthrough:末帧 usage-only chunk 由 SDK 自动注入
        if _chunk_typed.usage_metadata is not None:
            usage = extract_usage(_chunk_typed)
            if usage:
                writer({"usage_metadata": usage})

    return {"messages": [AIMessage(content="".join(parts))]}


async def call_main_llm(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    """主对话 LLM 节点,通过 ``get_stream_writer()`` 流式输出 chunk。

    委托 ``_stream_llm_chunks`` 公共协程消费 ``state["messages"]`` 并派发
    SSE 信号。LLM 通过 Runtime DI 的 settings 构造,参数与
    ``build_main_llm(settings)`` 一致。

    guidance 决定 ``intervention_type``:audit_state.guidance 非空时发
    ``InterventionType.guided``,否则不发。本节点不写 DB——``persist_ai_turn``
    由 ``api/me.py`` 的流式 generator 在图流结束后调用。

    Args:
        state: 当前 MainDialogueState,读 ``audit_state.guidance``。
        runtime: LangGraph Runtime,提供 ChatContextSchema。

    Returns:
        ``messages`` 字段更新 dict,内容为拼接后的 ``AIMessage``。
    """
    ctx = runtime.context
    guidance = state.get("audit_state", {}).get("guidance")
    return await _stream_llm_chunks(
        state,
        ctx,
        llm=build_main_llm(ctx.settings, http_async_client=ctx.shared_http_client),
        # 由 Role.MAIN 解析模型档,reasoning 字段提取路径与 main 角色绑定一致。
        profile=role_profile(Role.MAIN),
        intervention_type=InterventionType.guided if guidance is not None else None,
    )


async def call_crisis_llm(
    state: MainDialogueState,
    runtime: Runtime[ChatContextSchema],
) -> dict:
    """crisis 干预 LLM 节点,通过 ``get_stream_writer()`` 流式输出 chunk。

    委托 ``_stream_llm_chunks`` 公共协程。干预类型无条件发
    ``InterventionType.crisis``,告知 api 层 ``pipeline.py`` 当前轮走
    crisis 干预路径。

    Args:
        state: 当前 MainDialogueState(本节点未直接读字段)。
        runtime: LangGraph Runtime,提供 ChatContextSchema。

    Returns:
        ``messages`` 字段更新 dict,内容为拼接后的 ``AIMessage``。
    """
    ctx = runtime.context
    return await _stream_llm_chunks(
        state,
        ctx,
        llm=build_crisis_llm(ctx.settings, http_async_client=ctx.shared_http_client),
        # crisis 复用 main 角色绑定,模型档同步沿用 main。
        profile=role_profile(Role.MAIN),
        intervention_type=InterventionType.crisis,
    )


# ---------------------------------------------------------------------------
# 图工厂:构造 StateGraph 与节点 / 边 / 路由,返回编译后的 CompiledStateGraph
# ---------------------------------------------------------------------------


def build_main_graph() -> CompiledStateGraph:
    """构建主对话 LangGraph(5 节点 + 2 分支条件路由)。

    Returns:
        编译后的 ``CompiledStateGraph``,可直接用于 ``astream`` /
        ``ainvoke``,由 ``RuntimeResources.main_graph`` 持有并被
        ``api/me.py::chat_stream`` 调用。
    """
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
