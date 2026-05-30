"""me 路由：当前账号信息 / child profile / 会话管理。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.auth.deps import get_current_account, require_child
from app.auth.redis_client import get_redis
from app.chat.compression import CONTEXT_COMPRESS_THRESHOLD_TOKENS
from app.chat.context_schema import ChatContextSchema
from app.chat.graph import enqueue_audit
from app.chat.locks import (
    acquire_session_lock,
    acquire_throttle_lock,
    release_session_lock,
    running_streams,
)
from app.chat.prompts import build_system_prompt
from app.chat.session_policy import (
    SHANGHAI,
    logical_day,
    should_switch_session,
    today_session_title,
)
from app.chat.sse import build_flow_pause_frame, stream_graph_to_sse
from app.db import get_db
from app.models.accounts import ChildProfile, User
from app.models.chat import Message
from app.models.chat import Session as SessionModel
from app.models.enums import MessageRole, MessageStatus, SessionStatus
from app.runtime import RuntimeResources
from app.schemas.accounts import AccountOut, CurrentAccount
from app.schemas.children import ChildProfileOut
from app.schemas.sessions import (
    ChatStreamRequest,
    MessageListItem,
    MessageListResponse,
    SessionListItem,
    SessionListResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/me", tags=["me"])


@router.get("", response_model=AccountOut)
async def get_me(
    current: Annotated[CurrentAccount, Depends(get_current_account)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AccountOut:
    """返回当前登录账号的 AccountOut（供续期触发测试用）。"""
    user = await db.get(User, current.id)
    if user is None:
        raise HTTPException(404, "user not found")
    return AccountOut(
        id=user.id,
        role=user.role,
        family_id=user.family_id,
        phone=user.phone,
        is_active=user.is_active,
    )


@router.get("/profile", response_model=ChildProfileOut)
async def get_my_profile(
    current: Annotated[CurrentAccount, Depends(require_child)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ChildProfileOut:
    """子账号查询自身 ChildProfile；parent token → 403；profile 不存在 → 404。"""
    profile = (
        await db.execute(select(ChildProfile).where(ChildProfile.child_user_id == current.id))
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(404, "profile not found")
    assert profile.gender is not None
    assert profile.birth_date is not None
    return ChildProfileOut(
        id=profile.child_user_id,
        nickname=profile.nickname,
        gender=profile.gender.value,
        birth_date=profile.birth_date,
    )


# ---------------------------------------------------------------------------
# cursor helpers (keyset pagination)
# ---------------------------------------------------------------------------


class InvalidCursor(HTTPException):
    """Raised when cursor decoding fails: bad base64, bad ISO, or bad UUID."""

    def __init__(self) -> None:
        super().__init__(400, "InvalidCursor")


def _encode_cursor(sort_key: datetime, row_id: str) -> str:
    if sort_key.tzinfo is not None:
        sort_key = sort_key.replace(tzinfo=None)
    return base64.urlsafe_b64encode(f"{sort_key.isoformat()}|{row_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode cursor into (sort_key as naive datetime, row_id as str). Raises InvalidCursor."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    except Exception:
        raise InvalidCursor()

    parts = raw.rsplit("|", 1)
    if len(parts) != 2:
        raise InvalidCursor()
    sort_key_str, row_id = parts

    sort_key_dt: datetime | None = None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(sort_key_str, fmt)
            sort_key_dt = dt.replace(tzinfo=None)
            break
        except ValueError:
            continue
    _naive_fmts = (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    )
    for fmt in _naive_fmts:
        try:
            sort_key_dt = datetime.strptime(sort_key_str, fmt)
            break
        except ValueError:
            continue
    if sort_key_dt is None:
        raise InvalidCursor()

    try:
        UUID(row_id)
    except Exception:
        raise InvalidCursor()

    return sort_key_dt, row_id


# ---------------------------------------------------------------------------
# GET /me/sessions
# ---------------------------------------------------------------------------


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    current: Annotated[CurrentAccount, Depends(require_child)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=50)] = 15,
    cursor: str | None = None,
) -> SessionListResponse:
    """List sessions for the authenticated child (keyset pagination, no in_progress).

    M6-patch3：响应顶层附 today_session_id；sessions 数组过滤今日 logical_day。
    """
    if cursor is not None and cursor == "":
        cursor = None

    now = datetime.now(SHANGHAI)
    latest = (
        await db.execute(
            select(SessionModel)
            .where(SessionModel.child_user_id == current.id, SessionModel.status == "active")
            .order_by(SessionModel.last_active_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    today_sid = (
        latest.id if latest and logical_day(latest.last_active_at) == logical_day(now) else None
    )

    stmt = (
        select(SessionModel.id, SessionModel.title, SessionModel.last_active_at)
        .where(
            SessionModel.child_user_id == current.id,
            SessionModel.status == "active",
        )
        .order_by(SessionModel.last_active_at.desc(), SessionModel.id.desc())
        .limit(limit + 1)
    )
    if today_sid is not None:
        stmt = stmt.where(SessionModel.id != today_sid)
    if cursor:
        last_active_at_dt, sid = _decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(SessionModel.last_active_at, SessionModel.id) < (last_active_at_dt, sid),
        )
    result = await db.execute(stmt)
    rows = result.fetchall()

    has_more = len(rows) > limit
    items = rows[:limit]

    if has_more and items:
        last = items[-1]
        next_cursor = _encode_cursor(last.last_active_at, str(last.id))
    else:
        next_cursor = None

    return SessionListResponse(
        sessions=[
            SessionListItem(id=row.id, title=row.title, last_active_at=row.last_active_at)
            for row in items
        ],
        today_session_id=today_sid,
        next_cursor=next_cursor,
    )


# ---------------------------------------------------------------------------
# GET /me/sessions/{id}/messages
# ---------------------------------------------------------------------------


@router.get("/sessions/{sid}/messages", response_model=MessageListResponse)
async def get_messages(
    sid: str,
    current: Annotated[CurrentAccount, Depends(require_child)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> MessageListResponse:
    """Fetch messages for a session (keyset pagination, top-level in_progress)."""
    session_row = await db.get(SessionModel, sid)
    if session_row is None or session_row.status != "active":
        raise HTTPException(404, "SessionNotFound")
    if session_row.child_user_id != current.id:
        raise HTTPException(403, "SessionForbidden")

    if cursor is not None and cursor == "":
        cursor = None

    stmt = (
        select(
            Message.id,
            Message.role,
            Message.content,
            Message.status,
            Message.finish_reason,
            Message.created_at,
        )
        .where(
            Message.session_id == sid,
            Message.role.in_([MessageRole.human, MessageRole.ai]),
            Message.status != MessageStatus.discarded,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit + 1)
    )
    if cursor:
        created_at_dt, mid = _decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(Message.created_at, Message.id) < (created_at_dt, mid),
        )
    result = await db.execute(stmt)
    rows = result.fetchall()

    has_more = len(rows) > limit
    items = rows[:limit]

    if has_more and items:
        last = items[-1]
        next_cursor = _encode_cursor(last.created_at, str(last.id))
    else:
        next_cursor = None

    try:
        in_progress = bool(await redis.exists(f"chat:lock:{sid}"))
    except RedisError as e:
        in_progress = False
        logger.warning(
            "redis exists failed for chat:lock:%s, fallback in_progress=False: %s", sid, e
        )

    return MessageListResponse(
        items=[
            MessageListItem(
                id=row.id,
                role=row.role,
                content=row.content,
                status=row.status,
                finish_reason=row.finish_reason,
                created_at=row.created_at,
            )
            for row in items
        ],
        next_cursor=next_cursor,
        in_progress=in_progress,
    )


# ---------------------------------------------------------------------------
# DELETE /me/sessions/{id}
# ---------------------------------------------------------------------------


@router.delete("/sessions/{sid}", status_code=204)
async def delete_session(
    sid: str,
    current: Annotated[CurrentAccount, Depends(require_child)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Soft-delete a session (status='deleted'). Idempotent: second call → 404."""
    session = (
        await db.execute(select(SessionModel).where(SessionModel.id == sid))
    ).scalar_one_or_none()

    if session is None or session.status == "deleted":
        raise HTTPException(404, "SessionNotFound")

    if session.child_user_id != current.id:
        raise HTTPException(403, "SessionForbidden")

    session.status = SessionStatus.deleted
    await db.commit()


# ---------------------------------------------------------------------------
# POST /me/sessions/{id}/stop
# ---------------------------------------------------------------------------


@router.post("/sessions/{sid}/stop", status_code=204)
async def stop_session(
    sid: str,
    current: Annotated[CurrentAccount, Depends(require_child)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """停止正在运行的对话流（best-effort）。

    向 running_streams 中对应 sid 的 asyncio.Event 发送信号，使 generator 在
    下一次 yield 前退出。无论 event 是否存在都返回 204（best-effort 语义）。
    软删 session（status='deleted'）对客户端不可见，返回 404。
    """
    session = (
        await db.execute(
            select(SessionModel).where(
                SessionModel.id == sid,
                SessionModel.status == "active",
            )
        )
    ).scalar_one_or_none()

    if session is None:
        raise HTTPException(404, "SessionNotFound")

    if session.child_user_id != current.id:
        raise HTTPException(403, "SessionForbidden")

    event = running_streams.get(sid)
    if event is not None:
        event.set()
    # 始终返回 204（async best-effort，generator 后续在 finally 处理剩余清理）


# ---------------------------------------------------------------------------
# POST /me/chat/stream
# ---------------------------------------------------------------------------


def _frame_sse_event(event_type: str, data: dict) -> bytes:
    """SSE 多行协议帧（M6）：event: <type>\\ndata: <json>\\n\\n。"""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


async def _stub_stream() -> list[bytes]:
    """LLM 桩流：产生一个 delta 帧和一个 end 帧。"""
    return [
        _frame_sse_event("delta", {"content": "[stub]"}),
        _frame_sse_event("end", {"finish_reason": "stop", "aid": None}),
    ]


@dataclass
class _ChatStreamState:
    """段一段二共享的轻量 mutable container。"""

    overflow: bool = False


async def _run_llm_pipeline(
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
    state: _ChatStreamState,
    stop_event: asyncio.Event,
    protected_id: UUID | None = None,
    age: int = 8,
    gender: str | None = None,
) -> None:
    """段一：LLM consumption 协程，在独立 asyncio.Task 中运行。

    M9-patch1 解耦后，以下业务逻辑从 HTTP StreamingResponse generator
    迁入本函数（§7.3 清单 1-8）：compression → thinking 状态机 →
    graph.astream → stream_graph_to_sse 帧映射 → commit② 三终态
    （自然结束 / StopWithAi / StopNoAi）。

    accumulated 语义（关注点 #4）：以段一全量产出为准，不受段二
    客户端送达影响。即使客户端断连，commit② 仍将完整 accumulated
    落库，确保 Resume 流能取出完整 ai 行。
    """

    def _put(frame: bytes) -> None:
        """入队辅助：队满即翻 overflow flag（关注点 #3），后续 put 全部跳过。

        overflow 翻正后段一继续跑（graph 循环 + commit② 照常），
        仅停止入队。段二在取帧前检测 overflow 标志，自行发 flow_pause
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
    usage_meta: dict | None = None
    has_emitted_content = False
    user_stopped = False
    thinking_started = False

    try:
        async with rr.db_session_factory() as db:
            try:
                # 第一帧：session_meta
                _put(_frame_sse_event("session_meta", {"session_id": str(sid), "hid": str(hid)}))

                # 重新加载 session（段一有自己独立的 db session）
                session = await db.get(SessionModel, sid)
                if session is None:
                    raise RuntimeError(f"session {sid} not found")

                # ---- 阻塞压缩检查 ----
                if session.needs_compression:
                    try:
                        _put(_frame_sse_event("compression_start", {}))

                        from app.chat.compression import (
                            build_compression_prompt,
                            extract_compression_summary,
                        )
                        from app.chat.context import _to_lc_message
                        from app.chat.factory import build_provider_llm

                        assert protected_id is not None, "compression requires protected_id"

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

                        _put(_frame_sse_event("compression_end", {}))
                    except Exception:
                        logger.exception("compression failed for session %s", sid)
                        _put(
                            _frame_sse_event(
                                "error",
                                {"message": "压缩失败，请重试", "code": "CompressionError"},
                            )
                        )

                # ---- 图谱主循环 ----
                graph = rr.main_graph
                async for payload in graph.astream(
                    initial_state,
                    context=ctx,  # type: ignore[arg-type]
                    stream_mode="custom",
                ):
                    if stop_event.is_set():
                        user_stopped = True
                        break
                    if payload.get("usage_metadata"):
                        usage_meta = payload["usage_metadata"]

                    # reasoning 信号（关注点 #5：段一无 client_alive 门控，
                    # 帧无条件入队；段二 yield 时通过捕捉 ConnectionError 自行退役）
                    if payload.get("reasoning"):
                        if not thinking_started:
                            thinking_started = True
                            _put(_frame_sse_event("thinking_start", {}))
                        continue

                    d = payload.get("delta", "")
                    if d:
                        has_emitted_content = True
                        accumulated += d

                    # 首个非空 delta → 收 thinking
                    if d and thinking_started:
                        thinking_started = False
                        _put(_frame_sse_event("thinking_end", {}))

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

                # ---- commit② 三终态（关注点 #1） ----
                if user_stopped:
                    if has_emitted_content:
                        # StopWithAi：写 ai 行 + enqueue_audit + 带 aid 的 stopped 帧
                        ai_msg = Message(
                            session_id=sid,
                            role=MessageRole.ai,
                            content=accumulated,
                            status=MessageStatus.active,
                            finish_reason="user_stopped",
                            turn_number=turn_number,
                        )
                        db.add(ai_msg)
                        await db.flush()
                        aid = ai_msg.id
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
                            _frame_sse_event(
                                "stopped",
                                {"finish_reason": "user_stopped", "aid": str(aid)},
                            )
                        )
                    else:
                        # StopNoAi：不写 ai 行、不发 audit、不带 aid 的 stopped 帧
                        _put(_frame_sse_event("stopped", {"finish_reason": "user_stopped"}))
                else:
                    # 自然结束：写 ai 行 + enqueue_audit + end 帧带 aid
                    ai_msg = Message(
                        session_id=sid,
                        role=MessageRole.ai,
                        content=accumulated,
                        status=MessageStatus.active,
                        finish_reason=last_finish_reason,
                        turn_number=turn_number,
                    )
                    db.add(ai_msg)
                    await db.flush()
                    aid = ai_msg.id
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
                        _frame_sse_event(
                            "end",
                            {"finish_reason": last_finish_reason, "aid": str(aid)},
                        )
                    )

            except Exception as e:
                logger.exception("llm pipeline error", extra={"sid": str(sid)})
                await db.rollback()
                _put(_frame_sse_event("error", {"message": str(e), "code": "InternalError"}))

    finally:
        running_streams.pop(str(sid), None)
        if not state.overflow:
            try:
                queue.put_nowait(None)  # 哨兵：通知段二正常退出
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


async def _stream_generator(
    queue: asyncio.Queue,
    state: _ChatStreamState,
    sid: UUID,
) -> AsyncGenerator[bytes, None]:
    """段二：StreamingResponse generator。仅做帧转发 + overflow check + 客户端断检测。

    overflow check（关注点 #3）在 await queue.get() 之前，避免以下时序陷阱：
    段一 put_nowait → QueueFull → 翻 overflow → 段二从 queue.get() 取出后
    queue.full() 永远返回 False（size 已减 1），造成 overflow 漏检。

    首次帧超时保护：bg task（段一）在 async with db_session_factory 或 session_meta
    入队前静默崩溃时，queue 永远为空 → generator 无限阻塞。
    前 10 秒内必须产生至少一帧（session_meta），超时则静默退出，
    避免请求级永久挂死（关注点 #3 补充防护）。
    """
    try:
        first_frame = True
        while True:
            if state.overflow:
                yield build_flow_pause_frame("backpressure")
                logger.info("sse backpressure cutoff", extra={"sid": str(sid)})
                return

            try:
                if first_frame:
                    frame = await asyncio.wait_for(queue.get(), timeout=10.0)
                    first_frame = False
                else:
                    frame = await queue.get()
            except asyncio.TimeoutError:
                # 段一未能在 10s 内产生首帧（含 session_meta），
                # 大概率是 bg task startup 静默崩溃；静默退出不做错误帧（段一已负责日志）
                logger.error(
                    "first frame timeout, bg task may have crashed silently",
                    extra={"sid": str(sid)},
                )
                return

            if frame is None:
                break

            try:
                yield frame
            except ConnectionError, anyio.BrokenResourceError:
                logger.info("client disconnected", extra={"sid": str(sid)})
                return
    except asyncio.CancelledError:
        raise


@router.post("/chat/stream")
async def chat_stream(
    request: Request,
    req: ChatStreamRequest,
    current: Annotated[CurrentAccount, Depends(require_child)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> StreamingResponse:
    """流式接口：决策矩阵 → 提交① → 段一 bg task + 段二 generator。

    M9-patch1 解耦后：
    - 同步前置（限流 / sesssion 策略 / 决策矩阵 / commit①）保持原位
    - LLM consumption 迁至独立 _run_llm_pipeline（段一），
      通过 asyncio.Queue 单向中转 SSE 字节帧给段二
    - StreamingResponse 由 _stream_generator（段二）承担，仅做帧转发 +
      overflow check + 客户端断检测

    Decision matrix O (baseline §5.4, 7 rows) 参见 _run_llm_pipeline docstring。
    """
    # ---- throttle lock (TTL 自然过期，finally 不主动 DEL) ----
    if not await acquire_throttle_lock(redis, str(current.id)):
        raise HTTPException(429, "RequestThrottled")

    # ---- session policy resolution（确定生效 sid） ----
    now = datetime.now(SHANGHAI)
    latest = (
        await db.execute(
            select(SessionModel)
            .where(SessionModel.child_user_id == current.id, SessionModel.status == "active")
            .order_by(SessionModel.last_active_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    sid: UUID
    session: SessionModel
    is_new_session: bool

    if should_switch_session(latest.last_active_at if latest else None, now):
        sid = uuid4()
        session = SessionModel(
            id=sid,
            child_user_id=current.id,
            title=today_session_title(now),
            status="active",
            last_active_at=now,
        )
        db.add(session)
        is_new_session = True
    else:
        assert latest is not None, "已有 active session"
        sid = latest.id
        session = latest
        is_new_session = False

    # ---- session lock（新建 session 无 race，但为简化统一 lock） ----
    nonce = await acquire_session_lock(redis, str(sid))
    if not nonce:
        raise HTTPException(409, "SessionBusy")

    # 确保在 StreamingResponse 之前抛出 HTTPException 时释放锁
    try:
        # ---- session ownership check（仅复用 session 需验证） ----
        if not is_new_session and session.child_user_id != current.id:
            raise HTTPException(403, "SessionForbidden")

        # ---- 准备 child_profile 数据（无 DB 写入依赖） ----
        child_profile = await db.get(ChildProfile, current.id)
        if child_profile is not None:
            from app.chat.prompts import compute_age

            _age = compute_age(child_profile.birth_date)
            _gender = child_profile.gender.value if child_profile.gender else None
        else:
            _age = 8  # 兜底默认值
            _gender = None

        # ---- decision matrix O + first-turn / subsequent-turn transaction ----
        last_msg = (
            await db.execute(
                select(Message)
                .where(Message.session_id == sid, Message.status == MessageStatus.active)
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        # M9: turn_number = 下一轮号（commit① human + commit② ai 共享同号）
        _turn_number = (session.ai_turn_counter or 0) + 1

        hid: UUID  # human 消息 ID，用于 session_meta 事件
        user_msg: Message | None = None  # 追踪本轮新增的 human message，供 commit① 用

        if last_msg is None:
            # Row 1 或 Row 2
            if req.regenerate_for is not None:
                raise HTTPException(400, "RegenerateForInvalid")
            # Row 1：首轮（INSERT human active；session 已在策略解析中建好）
            human = Message(
                session_id=sid,
                role=MessageRole.human,
                status=MessageStatus.active,
                content=req.content,
                turn_number=_turn_number,
            )
            db.add(human)
            await db.flush()
            hid = human.id
            user_msg = human
        # Row 3：末条为 AI，regen=null → INSERT human
        elif last_msg.role == MessageRole.ai:
            if req.regenerate_for is not None:
                raise HTTPException(400, "RegenerateForInvalid")
            human = Message(
                session_id=sid,
                role=MessageRole.human,
                status=MessageStatus.active,
                content=req.content,
                turn_number=_turn_number,
            )
            db.add(human)
            await db.flush()
            hid = human.id
            user_msg = human
        else:
            # last_msg.role == MessageRole.human
            # Gate A 闭合论证：last_msg 是按 ORDER BY created_at DESC, id DESC LIMIT 1
            # 查询得到的"最新 active 行"。"非孤儿 human"意味着有一条 active AI 行
            # 严格排在它之后——但该 AI 行本身会成为"最新 active 行"，与 ORDER BY 结果矛盾。
            # 因此"末条 active 行是 human" ⟺ "孤儿 human"（穷举，无需二次查询）。
            is_orphan = last_msg.role == MessageRole.human

            if is_orphan:
                # Row 5、6、7
                if req.regenerate_for is None:
                    # Row 5：孤儿 + null → UPDATE 旧行 discarded + INSERT 新行
                    await db.execute(
                        update(Message)
                        .where(Message.id == last_msg.id)
                        .values(status=MessageStatus.discarded),
                    )
                    new_human = Message(
                        session_id=sid,
                        role=MessageRole.human,
                        status=MessageStatus.active,
                        content=req.content,
                        turn_number=_turn_number,
                    )
                    db.add(new_human)
                    await db.flush()
                    hid = new_human.id
                    user_msg = new_human
                elif req.regenerate_for == str(last_msg.id):
                    # Row 6：孤儿 + =hid → 复用孤儿行（不新增行，不更新内容）
                    if req.content != "":
                        raise HTTPException(400, "RegenerateForInvalid")
                    hid = last_msg.id
                    # user_msg 保持 None — 复用已有消息，不新增
                else:
                    # Row 7：孤儿 + ≠hid → 400
                    raise HTTPException(400, "RegenerateForInvalid")
            else:
                # 非孤儿 human 不可能成为末条 active 行（Gate A 闭合论证）。
                # 此分支不可达——raise 以捕获未来状态空间错误。
                raise AssertionError("unreachable: non-orphan human cannot be last active row")

        # build_context / build_system_prompt 已由 build_messages_main 节点在图内执行
        # commit① — user 消息落库（同事务内同步 last_active_at）
        if user_msg is not None:
            session.last_active_at = user_msg.created_at
        await db.commit()

        # ---- 流式响应（M9-patch1 解耦） ----
        # 段一：LLM consumption 独立 bg task；段二：StreamingResponse 帧转发
        rr: RuntimeResources = request.app.state.resources

        from app.chat.state import MainDialogueState

        ctx = ChatContextSchema(
            session_id=sid,
            child_user_id=current.id,
            child_profile={},
            age=_age,
            gender=_gender,
            user_input=req.content,
            settings=rr.settings,
            db_session_factory=rr.db_session_factory,
            audit_redis=rr.audit_redis,
        )

        initial_state: MainDialogueState = {
            "messages": [],
            "audit_state": {
                "crisis_locked": False,
                "crisis_detected": False,
                "redline_triggered": False,
                "guidance": None,
                "target_message_id": None,
            },
            "generated_token_count": 0,
            "client_alive": True,
            "user_stop_requested": False,
            "turn_number": _turn_number,
        }

        # 计算 compression protected_id（段一用独立 db session，无法访问 handler 的 ORM 对象）
        # Row 1（last_msg is None）无旧消息可压缩，protected_id 置 None
        _protected_id: UUID | None = None
        if last_msg is not None:
            _protected_id = user_msg.id if user_msg is not None else last_msg.id

        # ★ stop event 注册必须在 create_task 之前（避免 race）
        stop_event = asyncio.Event()
        running_streams[str(sid)] = stop_event

        _maxsize = rr.settings.chat_queue_maxsize
        if not isinstance(_maxsize, int):
            _maxsize = 128
        queue: asyncio.Queue = asyncio.Queue(maxsize=_maxsize)
        state = _ChatStreamState()

        bg = asyncio.create_task(
            _run_llm_pipeline(
                rr=rr,
                redis=redis,
                sid=sid,
                hid=hid,
                nonce=nonce,
                child_user_id=current.id,
                turn_number=_turn_number,
                initial_state=initial_state,
                ctx=ctx,
                queue=queue,
                state=state,
                stop_event=stop_event,
                protected_id=_protected_id,
                age=_age,
                gender=_gender,
            ),
            name=f"chat-llm-{sid}",
        )
        rr.register_chat_task(str(sid), bg)

        return StreamingResponse(
            _stream_generator(queue, state, sid),
            media_type="text/event-stream",
        )

    except HTTPException:
        # P0-2 锁释放：限流锁 TTL 自过期，不主动 DEL（关注点 #6）
        await release_session_lock(redis, str(sid), nonce)
        raise
    except Exception:
        # 非 HTTPException 异常路径（DB / Redis / OOM）同样释放锁，
        # 封死 commit①~create_task 间非 HTTPException 绕过 release_session_lock 的 lock 残留
        await release_session_lock(redis, str(sid), nonce)
        raise
