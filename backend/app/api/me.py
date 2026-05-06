"""me 路由：当前账号信息 / child profile / 会话管理。"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_account, require_child
from app.auth.redis_client import get_redis
from app.db import get_db
from app.models.accounts import ChildProfile, User
from app.models.chat import Session as SessionModel
from app.schemas.accounts import AccountOut, CurrentAccount
from app.schemas.children import ChildProfileOut
from app.schemas.sessions import (
    MessageListItem,
    MessageListResponse,
    SessionListItem,
    SessionListResponse,
)

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
    limit: Annotated[int, Query(ge=1, le=50)] = 15,
    cursor: str | None = None,
    current: Annotated[CurrentAccount, Depends(require_child)] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
) -> SessionListResponse:
    """List sessions for the authenticated child (keyset pagination, no in_progress)."""
    if cursor is not None and cursor == "":
        cursor = None

    where_clause = "child_user_id = :uid AND status = 'active'"
    args: dict = {"uid": str(current.id), "p_limit": limit + 1}

    if cursor:
        last_active_at_dt, sid = _decode_cursor(cursor)
        where_clause += " AND (last_active_at, id) < (:last_at, :sid)"
        args["last_at"] = last_active_at_dt
        args["sid"] = sid

    sql = (
        f"SELECT id, title, last_active_at FROM sessions "
        f"WHERE {where_clause} "
        f"ORDER BY last_active_at DESC, id DESC LIMIT :p_limit"
    )
    result = await db.execute(text(sql), args)
    rows = result.fetchall()

    has_more = len(rows) > limit
    items = rows[:limit]

    if has_more and items:
        last = items[-1]
        next_cursor = _encode_cursor(last.last_active_at, str(last.id))
    else:
        next_cursor = None

    return SessionListResponse(
        items=[
            SessionListItem(id=row.id, title=row.title, last_active_at=row.last_active_at)
            for row in items
        ],
        next_cursor=next_cursor,
    )


# ---------------------------------------------------------------------------
# GET /me/sessions/{id}/messages
# ---------------------------------------------------------------------------


@router.get("/sessions/{sid}/messages", response_model=MessageListResponse)
async def get_messages(
    sid: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    current: Annotated[CurrentAccount, Depends(require_child)] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
    redis: Annotated[Redis, Depends(get_redis)] = None,
) -> MessageListResponse:
    """Fetch messages for a session (keyset pagination, top-level in_progress)."""
    session_row = await db.get(SessionModel, sid)
    if session_row is None or session_row.status != "active":
        raise HTTPException(404, "SessionNotFound")
    if session_row.child_user_id != current.id:
        raise HTTPException(403, "SessionForbidden")

    if cursor is not None and cursor == "":
        cursor = None

    where_clause = "session_id = :sid AND status = 'active'"
    args: dict = {"sid": sid, "p_limit": limit + 1}

    if cursor:
        created_at_dt, mid = _decode_cursor(cursor)
        where_clause += " AND (created_at, id) < (:created_at, :mid)"
        args["created_at"] = created_at_dt
        args["mid"] = mid

    sql = (
        f"SELECT id, role, content, status, finish_reason, created_at FROM messages "
        f"WHERE {where_clause} "
        f"ORDER BY created_at DESC, id DESC LIMIT :p_limit"
    )
    result = await db.execute(text(sql), args)
    rows = result.fetchall()

    has_more = len(rows) > limit
    items = rows[:limit]

    if has_more and items:
        last = items[-1]
        next_cursor = _encode_cursor(last.created_at, str(last.id))
    else:
        next_cursor = None

    in_progress = bool(await redis.exists(f"chat:lock:{sid}"))

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
    current: Annotated[CurrentAccount, Depends(require_child)] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
) -> None:
    """Soft-delete a session (status='deleted'). Idempotent: second call → 404."""
    db.expunge_all()

    session = (
        await db.execute(select(SessionModel).where(SessionModel.id == sid))
    ).scalar_one_or_none()

    if session is None or session.status == "deleted":
        raise HTTPException(404, "SessionNotFound")

    if session.child_user_id != current.id:
        raise HTTPException(403, "SessionForbidden")

    await db.execute(
        text(
            "UPDATE sessions SET status = 'deleted' "
            "WHERE id = :sid AND child_user_id = :uid AND status = 'active'"
        ),
        {"sid": sid, "uid": str(current.id)},
    )
    await db.commit()
