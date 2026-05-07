"""me 路由：当前账号信息 / child profile / 会话管理。"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Annotated
from uuid import UUID, uuid4

import regex
from fastapi import APIRouter, Depends, HTTPException, Query
from redis.asyncio import Redis
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.auth.deps import get_current_account, require_child
from app.auth.redis_client import get_redis
from app.chat.locks import acquire_session_lock, acquire_throttle_lock, release_session_lock
from app.db import get_db
from app.models.accounts import ChildProfile, User
from app.models.chat import Message
from app.models.chat import Session as SessionModel
from app.models.enums import MessageRole, MessageStatus
from app.schemas.accounts import AccountOut, CurrentAccount
from app.schemas.children import ChildProfileOut
from app.schemas.sessions import (
    ChatStreamRequest,
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


# ---------------------------------------------------------------------------
# POST /me/chat/stream
# ---------------------------------------------------------------------------


def _truncate_title(content: str, max_graphemes: int = 12) -> str:
    """Truncate content to at most max_graphemes grapheme clusters via regex \\X."""
    graphemes = regex.findall(r"\X", content)
    return "".join(graphemes[:max_graphemes])


def _frame_sse_event(event_type: str, data: dict) -> bytes:
    """SSE multi-line protocol frame (M6): event: <type>\\ndata: <json>\\n\\n."""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


async def _stub_stream() -> list[bytes]:
    """Stub LLM stream: yields one delta and one end frame."""
    return [
        _frame_sse_event("delta", {"content": "[stub]"}),
        _frame_sse_event("end", {"finish_reason": "stop", "aid": None}),
    ]


@router.post("/chat/stream")
async def chat_stream(
    req: ChatStreamRequest,
    current: Annotated[CurrentAccount, Depends(require_child)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> StreamingResponse:
    """Stream a child dialogue turn (control plane + stub LLM stream, Step 8a).

    Flow: throttle lock → session existence check → session lock → decision-O
    matrix → first-turn / subsequent-turn transaction → StreamingResponse.

    Decision matrix O (baseline §5.4, 7 rows):
      Row 1: last=None   + regen=null  → INSERT session + INSERT human (active)
      Row 2: last=None   + regen=!null → 400 RegenerateForInvalid
      Row 3: last=AI      + regen=null  → INSERT human (active)
      Row 4: last=AI      + regen=!null → 400 RegenerateForInvalid  (ai row不可重生)
      Row 5: last=orphan  + regen=null  → UPDATE old discarded + INSERT human (active)
      Row 6: last=orphan  + regen=hid   → reuse orphan (no new row, content must be "")
      Row 7: last=orphan  + regen=!hid  → 400 RegenerateForInvalid  (历史轮不可重生)

    Gate A closing argument (applies to rows 5-7):
      "Last active message" is defined as SELECT ... WHERE status='active'
      ORDER BY created_at DESC, id DESC LIMIT 1 — it is always the latest active row.
      A "non-orphan human" would require an active AI row strictly after it,
      which would itself be the latest active row — contradicting the definition.
      Therefore "last active row is human" ⟺ "orphan human"; no second query needed.
      Rows 8/9 (non-orphan human paths) are unreachable — raise AssertionError
      if ever reached, to catch future state-space regressions.

    Lock-release contract: session lock is released in the generator finally
    block for the success path.  HTTPException raised before StreamingResponse
    construction is caught here and releases the lock explicitly (P0-2).
    """
    # ---- throttle lock (TTL 自然过期，finally 不主动 DEL) ----
    if not await acquire_throttle_lock(redis, str(current.id)):
        raise HTTPException(429, "RequestThrottled")

    # ---- resolve / generate session id ----
    sid: UUID
    if req.session_id:
        sid = UUID(req.session_id)
    else:
        sid = uuid4()

    # ---- session lock: must release on every HTTPException path (P0-2) ----
    nonce = await acquire_session_lock(redis, str(sid))
    if not nonce:
        raise HTTPException(409, "SessionBusy")

    # Ensure lock is released when HTTPException is raised before StreamingResponse
    try:
        # ---- session existence + child ownership check (done before SETNX: no lock leak) ----
        if req.session_id:
            session_row = await db.get(SessionModel, sid)
            if session_row is None:
                raise HTTPException(404, "SessionNotFound")
            if session_row.child_user_id != current.id:
                raise HTTPException(403, "SessionForbidden")

        # ---- decision matrix O + first-turn / subsequent-turn transaction ----
        last_msg = (
            await db.execute(
                select(Message)
                .where(Message.session_id == sid, Message.status == MessageStatus.active)
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        hid: UUID  # human message id for session_meta event

        if last_msg is None:
            # Row 1 or Row 2
            if req.regenerate_for is not None:
                raise HTTPException(400, "RegenerateForInvalid")
            # Row 1: first turn (INSERT human active)
            title = _truncate_title(req.content)
            session = SessionModel(
                id=sid,
                child_user_id=current.id,
                title=title,
                status=MessageStatus.active,
            )
            db.add(session)
            human = Message(
                session_id=sid,
                role=MessageRole.human,
                status=MessageStatus.active,
                content=req.content,
            )
            db.add(human)
            await db.flush()
            hid = human.id
        # Row 3: last is AI, regen=null → INSERT human
        elif last_msg.role == MessageRole.ai:
            if req.regenerate_for is not None:
                raise HTTPException(400, "RegenerateForInvalid")
            human = Message(
                session_id=sid,
                role=MessageRole.human,
                status=MessageStatus.active,
                content=req.content,
            )
            db.add(human)
            await db.flush()
            hid = human.id
        else:
            # last_msg.role == MessageRole.human
            # Closing argument (Gate A): last_msg is the LATEST active row by
            # ORDER BY created_at DESC, id DESC LIMIT 1.  A "non-orphan human" means
            # an active AI row *strictly after* it — but that AI would itself be the
            # "latest active row", contradicting the ORDER BY result.  Therefore
            # "last active row is human" ⟺ "orphan" (exhaustive, no second query needed).
            is_orphan = last_msg.role == MessageRole.human

            if is_orphan:
                # Rows 5, 6, 7
                if req.regenerate_for is None:
                    # Row 5: orphan + null → UPDATE old discarded + INSERT new
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
                    )
                    db.add(new_human)
                    await db.flush()
                    hid = new_human.id
                elif req.regenerate_for == str(last_msg.id):
                    # Row 6: orphan + =hid → reuse orphan (no new row, no content update)
                    # Option A: assert content is empty (strict contract)
                    if req.content != "":
                        raise HTTPException(400, "RegenerateForInvalid")
                    hid = last_msg.id
                else:
                    # Row 7: orphan + ≠hid → 400
                    raise HTTPException(400, "RegenerateForInvalid")
            else:
                # Non-orphan human cannot be last active row (Gate A closing argument).
                # This branch is unreachable — raise to catch future state-space bugs.
                raise AssertionError(
                    "unreachable: non-orphan human cannot be last active row"
                )

        await db.commit()

        # ---- streaming response ----
        async def generator() -> object:
            try:
                yield _frame_sse_event("session_meta", {"session_id": str(sid), "hid": str(hid)})
                for frame in await _stub_stream():
                    yield frame
            finally:
                await release_session_lock(redis, str(sid), nonce)

        return StreamingResponse(generator(), media_type="text/event-stream")

    except HTTPException:
        await release_session_lock(redis, str(sid), nonce)
        raise
