"""me 路由：当前账号信息 / child profile / 会话管理。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.core.db import get_db
from app.core.enums import MessageRole, MessageStatus, SessionStatus
from app.core.locks import (
    CHAT_LOCK_KEY_PREFIX,
    acquire_session_lock,
    acquire_throttle_lock,
    release_session_lock,
)
from app.core.redis import get_redis
from app.core.runtime import RuntimeResources
from app.core.time import SHANGHAI, age_at
from app.domain.accounts.models import ChildProfile, User
from app.domain.accounts.schemas import (
    AccountOut,
    ChildProfileOut,
    ChildProfileSnapshot,
    CurrentAccount,
)
from app.domain.auth.deps import get_current_account, require_child
from app.domain.chat.context_schema import ChatContextSchema
from app.domain.chat.models import Message
from app.domain.chat.models import Session as SessionModel
from app.domain.chat.pagination import decode_cursor, encode_cursor
from app.domain.chat.pipeline import run_llm_pipeline
from app.domain.chat.schemas import (
    ChatStreamRequest,
    MessageListItem,
    MessageListResponse,
    SessionListItem,
    SessionListResponse,
)
from app.domain.chat.session_policy import (
    should_switch_session,
    today_session_title,
)
from app.domain.chat.stream import ChatStreamState, stream_generator
from app.domain.chat.stream_signals import running_streams
from app.domain.chat.turn_intake import intake_human_message

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/me", tags=["me"])


# ---------------------------------------------------------------------------
# 辅助函数 (helpers)
# ---------------------------------------------------------------------------

# 路由 (对外暴露接口 / external API endpoints)
# ---------------------------------------------------------------------------


@router.get("", response_model=AccountOut)
async def get_me(
    current: Annotated[CurrentAccount, Depends(get_current_account)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AccountOut:
    """返回当前登录账号的 AccountOut（供续期触发测试用）。"""
    user = await db.get(User, current.id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "profile not found")
    return ChildProfileOut(
        child_user_id=profile.child_user_id,
        nickname=profile.nickname,
        gender=profile.gender.value,
        birth_date=profile.birth_date,
    )


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions(
    current: Annotated[CurrentAccount, Depends(require_child)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=50)] = 15,
    cursor: str | None = None,
) -> SessionListResponse:
    """List sessions for the authenticated child (keyset pagination, no in_progress).

    M6-patch3：响应顶层附 today_session_id；sessions 数组过滤今日(由 today_session_id 标识)。
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
    # 与 chat/stream 入口保持一致:hard cut + 凌晨空闲 30min 软切(should_switch_session)。
    # 仅靠逻辑日相等判定会漏掉凌晨空闲场景,导致 list 暴露的 today_session_id
    # 与 chat/stream 实际新建的 session 不一致(客户端跳到旧 session 反而触发新建)。
    today_sid = (
        latest.id if latest and not should_switch_session(latest.last_active_at, now) else None
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
        last_active_at_dt, sid = decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(SessionModel.last_active_at, SessionModel.id) < (last_active_at_dt, sid),
        )
    result = await db.execute(stmt)
    rows = result.fetchall()

    has_more = len(rows) > limit
    items = rows[:limit]

    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor(last.last_active_at, str(last.id))
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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SessionNotFound")
    if session_row.child_user_id != current.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "SessionForbidden")

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
        created_at_dt, mid = decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(Message.created_at, Message.id) < (created_at_dt, mid),
        )
    result = await db.execute(stmt)
    rows = result.fetchall()

    has_more = len(rows) > limit
    items = rows[:limit]

    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor(last.created_at, str(last.id))
    else:
        next_cursor = None

    try:
        in_progress = bool(await redis.exists(f"{CHAT_LOCK_KEY_PREFIX}{sid}"))
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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SessionNotFound")

    if session.child_user_id != current.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "SessionForbidden")

    session.status = SessionStatus.deleted
    await db.commit()


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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SessionNotFound")

    if session.child_user_id != current.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "SessionForbidden")

    event = running_streams.get(sid)
    if event is not None:
        event.set()
    # 始终返回 204（async best-effort，generator 后续在 finally 处理剩余清理）


@router.post("/chat/stream")
async def chat_stream(
    request: Request,
    req: ChatStreamRequest,
    current: Annotated[CurrentAccount, Depends(require_child)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> StreamingResponse:
    """流式接口：决策矩阵 → 提交① → 段一 bg task + 段二 generator。

    连接管理：前置 DB 工作收在一个短作用域 `db_session_factory` 块内，
    块退出即还连接到池，不横跨 StreamingResponse（Row 6 / regenerate
    无写入路径也走 commit① + 块退出回滚/还池，不留 idle in transaction）。
    DB 块有独立 try/except，流式设置段也有独立 try/except，
    sid 显式初始化为 None，杜绝未绑定风险。
    """
    rr: RuntimeResources = request.app.state.resources

    # ---- throttle lock (TTL 自然过期，finally 不主动 DEL) ----
    if not await acquire_throttle_lock(redis, str(current.id)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "RequestThrottled")

    nonce: str | None = None
    sid: UUID | None = None

    # ==================================================================
    # 短作用域 DB 块：前置工作 + 锁获取 + commit①
    # 块退出即还连接到池，不横跨 StreamingResponse
    # ==================================================================
    try:
        async with rr.db_session_factory() as db:
            # ---- session policy resolution（确定生效 sid） ----
            now = datetime.now(SHANGHAI)
            latest = (
                await db.execute(
                    select(SessionModel)
                    .where(
                        SessionModel.child_user_id == current.id,
                        SessionModel.status == "active",
                    )
                    .order_by(SessionModel.last_active_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

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

            # ---- session lock ----
            nonce = await acquire_session_lock(redis, str(sid))
            if not nonce:
                raise HTTPException(status.HTTP_409_CONFLICT, "SessionBusy")

            # ---- session ownership check（仅复用 session 需验证） ----
            if not is_new_session and session.child_user_id != current.id:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "SessionForbidden")

            # ---- 准备 child_profile 数据 ----
            child_profile = await db.scalar(
                select(ChildProfile).where(ChildProfile.child_user_id == current.id)
            )
            if child_profile is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "ChildProfileNotFound")

            profile_snapshot = ChildProfileSnapshot(
                child_user_id=child_profile.child_user_id,
                nickname=child_profile.nickname,
                gender=child_profile.gender.value,
                birth_date=child_profile.birth_date,
                age=age_at(child_profile.birth_date, tz="Asia/Shanghai"),
                sensitivity=child_profile.sensitivity,
                custom_redlines=child_profile.custom_redlines,
            )

            # ---- decision matrix O + first-turn / subsequent-turn transaction ----
            result = await intake_human_message(db, sid, session, req)

            # commit① — user 消息落库(同事务内同步 last_active_at)
            if result.user_msg is not None:
                session.last_active_at = result.user_msg.created_at
            await db.commit()
    except HTTPException:
        # nonce 在 sid 之后赋值，非 None 时 sid 必已绑定
        if nonce is not None:
            await release_session_lock(redis, str(sid), nonce)
        raise
    except Exception:
        if nonce is not None:
            await release_session_lock(redis, str(sid), nonce)
        raise
    # ★ db 块退出 → 连接已还池

    # # === DB 短作用域成功退出，sid / nonce 均已赋值 ===
    # assert sid is not None
    # assert nonce is not None

    # ==================================================================
    # 流式响应（不持 DB 连接）
    # ==================================================================
    try:
        from app.domain.chat.state import MainDialogueState

        ctx = ChatContextSchema(
            session_id=sid,
            child_user_id=current.id,
            child_profile=profile_snapshot,
            user_input=(
                result.regen_user_input if result.regen_user_input is not None else req.content
            ),
            settings=rr.settings,
            db_session_factory=rr.db_session_factory,
            audit_redis=rr.audit_redis,
        )

        initial_state: MainDialogueState = {
            "messages": [],
            "audit_state": {
                "crisis_locked": False,
                "crisis_detected": False,
                "guidance": None,
                "target_message_id": None,
            },
            "turn_number": result.turn_number,
            "compression_summary": None,
            "keep_messages": None,
        }

        # ★ stop event 注册必须在 create_task 之前（避免 race）
        stop_event = asyncio.Event()
        running_streams[str(sid)] = stop_event

        _maxsize = rr.settings.chat_queue_maxsize
        queue: asyncio.Queue = asyncio.Queue(maxsize=_maxsize)
        state = ChatStreamState()

        bg = asyncio.create_task(
            run_llm_pipeline(
                rr=rr,
                redis=redis,
                sid=sid,
                hid=result.hid,
                nonce=nonce,
                child_user_id=current.id,
                turn_number=result.turn_number,
                initial_state=initial_state,
                ctx=ctx,
                queue=queue,
                state=state,
                stop_event=stop_event,
            ),
            name=f"chat-llm-{sid}",
        )
        rr.register_chat_task(str(sid), bg)

        return StreamingResponse(
            stream_generator(queue, state, sid),
            media_type="text/event-stream",
        )
    except HTTPException:
        await release_session_lock(redis, str(sid), nonce)
        raise
    except Exception:
        await release_session_lock(redis, str(sid), nonce)
        raise
