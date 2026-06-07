"""段一:LLM consumption 协程。

Phase 2.3 从 `api/me.py` 抽离,在独立 asyncio.Task 中运行,负责:
compression → thinking 状态机 → graph.astream → stream_graph_to_sse
帧映射 → commit② 三终态(自然结束 / StopWithAi / StopNoAi)。

本模块只暴露 `run_llm_pipeline` 一个公开协程,其他均为内部实现。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select

from app.chat.compression import CONTEXT_COMPRESS_THRESHOLD_TOKENS
from app.chat.context_schema import ChatContextSchema
from app.chat.graph import enqueue_audit, persist_ai_turn
from app.chat.prompts import build_system_prompt
from app.core.locks import release_session_lock
from app.core.runtime import RuntimeResources
from app.domain.chat.stream import (
    ChatStreamState,
    frame_sse_event,
    stream_graph_to_sse,
)
from app.domain.chat.stream_signals import running_streams
from app.models.chat import Message
from app.models.chat import Session as SessionModel
from app.models.enums import InterventionType, MessageRole, MessageStatus

logger = logging.getLogger(__name__)


async def run_llm_pipeline(
    rr: RuntimeResources,
    redis: Redis,
    sid: UUID,
    hid: UUID,
    nonce: str,
    child_user_id: UUID,
    turn_number: int,
    initial_state: Any,
    ctx: ChatContextSchema,
    queue: asyncio.Queue,
    state: ChatStreamState,
    stop_event: asyncio.Event,
    protected_id: UUID | None = None,
    age: int = 8,
    gender: str | None = None,
) -> None:
    """段一:LLM consumption 协程,在独立 asyncio.Task 中运行。

    M9-patch1 解耦后,以下业务逻辑从 HTTP StreamingResponse generator
    迁入本函数(§7.3 清单 1-8):compression → thinking 状态机 →
    graph.astream → stream_graph_to_sse 帧映射 → commit② 三终态
    (自然结束 / StopWithAi / StopNoAi)。

    accumulated 语义(关注点 #4):以段一全量产出为准,不受段二
    客户端送达影响。即使客户端断连,commit② 仍将完整 accumulated
    落库,确保 Resume 流能取出完整 ai 行。
    """

    def _put(frame: bytes) -> None:
        """入队辅助:队满即翻 overflow flag(关注点 #3),后续 put 全部跳过。

        overflow 翻正后段一继续跑(graph 循环 + commit② 照常),
        仅停止入队。段二在取帧前检测 overflow 标志,自行发 flow_pause
        断流并退出。
        """
        if state.overflow:
            return
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            state.overflow = True
            logger.info("queue overflow, headless mode", extra={"sid": str(sid)})

    accumulated = ""
    last_finish_reason = "stop"
    last_intervention_type: InterventionType | None = None
    usage_meta: dict | None = None
    has_emitted_content = False
    user_stopped = False
    thinking_started = False

    try:
        async with rr.db_session_factory() as db:
            try:
                # 第一帧:session_meta
                _put(frame_sse_event("session_meta", {"session_id": str(sid), "hid": str(hid)}))

                # 重新加载 session(段一有自己独立的 db session)
                session = await db.get(SessionModel, sid)
                if session is None:
                    raise RuntimeError(f"session {sid} not found")

                # ---- 阻塞压缩检查 ----
                if session.needs_compression:
                    try:
                        _put(frame_sse_event("compression_start", {}))

                        from app.chat.compression import (
                            build_compression_prompt,
                            extract_compression_summary,
                        )
                        from app.chat.context import _to_lc_message
                        from app.chat.factory import build_provider_llm

                        if protected_id is None:
                            raise RuntimeError(
                                "compression triggered but no prior message to protect; "
                                "this should not happen in production"
                            )

                        actives_orm = (
                            (
                                await db.execute(
                                    select(Message)
                                    .where(
                                        Message.session_id == sid,
                                        Message.status == "active",
                                        Message.id != protected_id,
                                    )
                                    .order_by(Message.created_at.asc())
                                )
                            )
                            .scalars()
                            .all()
                        )

                        if actives_orm:
                            c_input = build_compression_prompt(
                                [_to_lc_message(mo) for mo in actives_orm]
                            )
                            c_llm = build_provider_llm(
                                f"compression_{rr.settings.compression_provider}",
                                rr.settings,
                            )
                            c_result = await c_llm.ainvoke(c_input)
                            raw = (
                                c_result.content if hasattr(c_result, "content") else str(c_result)
                            )
                            summary = extract_compression_summary(raw)
                            for mo in actives_orm:
                                mo.status = MessageStatus.compressed
                            db.add(
                                Message(
                                    session_id=sid,
                                    role=MessageRole.summary,
                                    status=MessageStatus.active,
                                    content=summary,
                                )
                            )
                        else:
                            logger.info(
                                "compression noop for session %s: no messages to compress", sid
                            )

                        session.needs_compression = False
                        await db.commit()

                        # 手动构造 initial_state["messages"]
                        _sp = build_system_prompt(age, gender)
                        _new_hist = []

                        if actives_orm:
                            _summary_msg = (
                                await db.execute(
                                    select(Message)
                                    .where(
                                        Message.session_id == sid,
                                        Message.role == MessageRole.summary,
                                        Message.status == MessageStatus.active,
                                    )
                                    .order_by(Message.created_at.desc())
                                    .limit(1)
                                )
                            ).scalar_one()
                            _new_hist.append(_to_lc_message(_summary_msg))

                        _protected_msg = (
                            await db.execute(select(Message).where(Message.id == protected_id))
                        ).scalar_one()
                        _new_hist.append(_to_lc_message(_protected_msg))

                        initial_state["messages"] = [_sp, *_new_hist]

                        _put(frame_sse_event("compression_end", {}))
                    except Exception:
                        logger.exception("compression failed for session %s", sid)
                        _put(
                            frame_sse_event(
                                "error",
                                {"message": "压缩失败,请重试", "code": "CompressionError"},
                            )
                        )

                # ---- 图谱主循环 ----
                graph = rr.main_graph
                async for payload in graph.astream(
                    initial_state,
                    context=ctx,  # type: ignore[arg-type]
                    stream_mode="custom",
                    # LangSmith trace 配置:按 session_id / child_id 过滤 trace。
                    # 当前调用点原本无 config,无既有键需合并(无 checkpointer /
                    # callbacks / configurable 既有键)。
                    config={
                        "run_name": "main_chat",
                        "metadata": {
                            "session_id": str(ctx.session_id),
                            "child_id": str(ctx.child_user_id),
                            "turn_number": turn_number,
                        },
                        "tags": ["main_chat"],
                    },
                ):
                    if stop_event.is_set():
                        user_stopped = True
                        break
                    if payload.get("usage_metadata"):
                        usage_meta = payload["usage_metadata"]

                    # reasoning 信号(关注点 #5:段一无 client_alive 门控,
                    # 帧无条件入队;段二 yield 时通过捕捉 ConnectionError 自行退役)
                    if payload.get("reasoning"):
                        if not thinking_started:
                            thinking_started = True
                            _put(frame_sse_event("thinking_start", {}))
                        continue

                    d = payload.get("delta", "")
                    if d:
                        has_emitted_content = True
                        accumulated += d

                    # 首个非空 delta → 收 thinking
                    if d and thinking_started:
                        thinking_started = False
                        _put(frame_sse_event("thinking_end", {}))

                    # intervention_type 信号(graph 终端节点在首 delta 前发射)。
                    # 此处 payload 是 graph 终端节点在 LLM .astream() 之前写入的单次路由帧,
                    # 严格早于后续 delta chunk 帧。_put 在此发射 SSE 事件后,
                    # 待第一个 delta 到达(如果有)才通过 stream_graph_to_sse 映射。
                    it_raw = payload.get("intervention_type")
                    if it_raw:
                        try:
                            last_intervention_type = InterventionType(it_raw)
                            _put(frame_sse_event("intervention_type", {"type": it_raw}))
                        except ValueError:
                            logger.warning(
                                "unknown intervention_type %r, falling back to None",
                                it_raw,
                            )
                            last_intervention_type = None

                    fr = payload.get("finish_reason")
                    if fr:
                        last_finish_reason = fr

                    # single-delta wrapper → stream_graph_to_sse 映射
                    async def _wrap():
                        yield payload

                    try:
                        async for frame in stream_graph_to_sse(_wrap()):
                            _put(frame)
                    except Exception:
                        logger.exception(
                            "stream_graph_to_sse mapping failed",
                            extra={"sid": str(sid)},
                        )

                # ---- commit② 三终态(关注点 #1) ----
                if user_stopped:
                    if has_emitted_content:
                        # StopWithAi:persist_ai_turn 写 ai 行 + 自增 → usage 记账 → commit → audit
                        aid = await persist_ai_turn(
                            db,
                            sid,
                            content=accumulated,
                            finish_reason="user_stopped",
                            turn_number=turn_number,
                            intervention_type=last_intervention_type,
                        )
                        if usage_meta:
                            _usage_total = usage_meta["input_tokens"] + usage_meta["output_tokens"]
                            session.context_size_tokens = _usage_total
                            if _usage_total >= CONTEXT_COMPRESS_THRESHOLD_TOKENS:
                                session.needs_compression = True
                        await db.commit()
                        await enqueue_audit(
                            rr.arq_pool,
                            rr.audit_redis,
                            sid,
                            db,
                            turn_number,
                            child_user_id,
                            aid,
                        )
                        _put(
                            frame_sse_event(
                                "stopped",
                                {"finish_reason": "user_stopped", "aid": str(aid)},
                            )
                        )
                    else:
                        # StopNoAi:不写 ai 行、不发 audit、不带 aid 的 stopped 帧
                        _put(frame_sse_event("stopped", {"finish_reason": "user_stopped"}))
                else:
                    # 自然结束:persist_ai_turn 写 ai 行 + 自增 → usage 记账 → commit → audit
                    aid = await persist_ai_turn(
                        db,
                        sid,
                        content=accumulated,
                        finish_reason=last_finish_reason,
                        turn_number=turn_number,
                        intervention_type=last_intervention_type,
                    )
                    if usage_meta:
                        _usage_total = usage_meta["input_tokens"] + usage_meta["output_tokens"]
                        session.context_size_tokens = _usage_total
                        if _usage_total >= CONTEXT_COMPRESS_THRESHOLD_TOKENS:
                            session.needs_compression = True
                    await db.commit()
                    await enqueue_audit(
                        rr.arq_pool,
                        rr.audit_redis,
                        sid,
                        db,
                        turn_number,
                        child_user_id,
                        aid,
                    )
                    _put(
                        frame_sse_event(
                            "end",
                            {"finish_reason": last_finish_reason, "aid": str(aid)},
                        )
                    )

            except Exception as e:
                logger.exception("llm pipeline error", extra={"sid": str(sid)})
                await db.rollback()
                _put(frame_sse_event("error", {"message": str(e), "code": "InternalError"}))

    finally:
        running_streams.pop(str(sid), None)
        if not state.overflow:
            try:
                queue.put_nowait(None)  # 哨兵:通知段二正常退出
            except asyncio.QueueFull:
                state.overflow = True
        try:
            await release_session_lock(redis, str(sid), nonce)
        except Exception:
            logger.warning(
                "release lock failed, rely on TTL",
                exc_info=True,
                extra={"sid": str(sid)},
            )
