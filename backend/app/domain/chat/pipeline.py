"""段一:LLM consumption 协程。

Phase 2.3 从 `api/me.py` 抽离,在独立 asyncio.Task 中运行,负责:
graph.astream 循环 → commit② 三终态(自然结束 / StopWithAi / StopNoAa)。

graph.astream 循环内部嵌套:
- thinking 状态机（reasoning 信号触发 thinking_start/thinking_end）
- stream_graph_to_sse 帧映射（per-payload wrapper）

本模块只暴露 `run_llm_pipeline` 一个公开协程,其他均为内部实现。

M9-patch2: 压缩逻辑已迁入 graph.py 图内压缩节点,删除图前阻塞压缩;
DB 会话拆分:图前短连接读 session,图循环不持有连接,图后短连接 commit②。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from app.core.enums import InterventionType
from app.core.locks import release_session_lock
from app.core.runtime import RuntimeResources
from app.domain.chat.compression import CONTEXT_COMPRESS_THRESHOLD_TOKENS
from app.domain.chat.context_schema import ChatContextSchema
from app.domain.chat.models import Session as SessionModel
from app.domain.chat.stream import (
    ChatStreamState,
    frame_sse_event,
    stream_graph_to_sse,
)
from app.domain.chat.stream_signals import running_streams
from app.domain.chat.usecase import enqueue_audit, persist_ai_turn

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
) -> None:
    """段一:LLM consumption 协程,在独立 asyncio.Task 中运行。

    M9-patch1 解耦后,以下业务逻辑从 HTTP StreamingResponse generator
    迁入本函数(§7.3 清单 1-8):thinking 状态机 →
    graph.astream → stream_graph_to_sse 帧映射 → commit② 三终态
    (自然结束 / StopWithAi / StopNoAi)。

    M9-patch2: 删除图前阻塞压缩(图内压缩节点覆盖);DB 会话拆为三段
    (图前短连接 → 图循环不持连接 → 图后短连接 commit②)。

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
        # ---- ① 图前:发送 session_meta（sid 在 commit① 已确认存在，无需查 DB） ----
        _put(frame_sse_event("session_meta", {"session_id": str(sid), "hid": str(hid)}))

        # ---- ② 图循环:不持有 DB 连接 ----
        graph = rr.main_graph
        async for payload in graph.astream(
            initial_state,
            context=ctx,  # type: ignore[arg-type]
            stream_mode="custom",
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

            # compression 信号(图内压缩节点发射)
            cs = payload.get("compression_start")
            if cs is not None:
                _put(frame_sse_event("compression_start", {}))
                continue

            ce = payload.get("compression_end")
            if ce is not None:
                _put(frame_sse_event("compression_end", {}))
                continue

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

        # ---- ③ 图后:短连接做 commit② ----
        async with rr.db_session_factory() as db:
            session = await db.get(SessionModel, sid)
            if session is None:
                raise RuntimeError(f"session {sid} not found in commit②")

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
                        ctx.child_profile,
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
                    ctx.child_profile,
                )
                _put(
                    frame_sse_event(
                        "end",
                        {"finish_reason": last_finish_reason, "aid": str(aid)},
                    )
                )

    except Exception as e:
        logger.exception("llm pipeline error", extra={"sid": str(sid)})
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
