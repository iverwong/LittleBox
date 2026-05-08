"""me 路由：当前账号信息 / child profile / 会话管理。"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from collections.abc import AsyncGenerator
from typing import Annotated
from uuid import UUID, uuid4

import regex
from fastapi import APIRouter, Depends, HTTPException, Query
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.auth.deps import get_current_account, require_child
from app.auth.redis_client import get_redis
from app.chat.graph import main_graph, persist_ai_turn
from app.chat.locks import acquire_session_lock, acquire_throttle_lock, release_session_lock
from app.chat.sse import stream_graph_to_sse
from app.db import get_db
from app.models.accounts import ChildProfile, User
from app.models.chat import Message
from app.models.chat import Session as SessionModel
from app.models.enums import MessageRole, MessageStatus, SessionStatus
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
    """List sessions for the authenticated child (keyset pagination, no in_progress)."""
    if cursor is not None and cursor == "":
        cursor = None

    stmt = (
        select(SessionModel.id, SessionModel.title, SessionModel.last_active_at)
        .where(
            SessionModel.child_user_id == current.id,
            SessionModel.status == "active",
        )
        .order_by(SessionModel.last_active_at.desc(), SessionModel.id.desc())
        .limit(limit + 1)
    )
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
            Message.id, Message.role, Message.content,
            Message.status, Message.finish_reason, Message.created_at,
        )
        .where(
            Message.session_id == sid,
            Message.status == MessageStatus.active,
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
    """Stream a child dialogue turn: decision matrix + graph stream + T5 persist.

    Flow: throttle lock → session check → session lock → decision-O
    matrix → first/subsequent-turn transaction → commit① → graph stream
    (main_graph.astream custom-mode) → stream_graph_to_sse → T5
    (persist_ai_turn writes ai active row) → commit② → StreamingResponse.

    Dual commit boundary: commit① (L489) persists human rows + session;
    commit② (L532-533 inside generator) persists the AI row
    (persist_ai_turn flush + db.commit).  Two commits = two atomic
    units: no ai row written on error path; no human row rolled back
    if graph stream fails.

    Decision matrix O (baseline §5.4, 7 rows):
      Row 1: last=None   + regen=null  → INSERT session + INSERT human (active)
      Row 2: last=None   + regen=!null → 400 RegenerateForInvalid
      Row 3: last=AI      + regen=null  → INSERT human (active)
      Row 4: last=AI      + regen=!null → 400 RegenerateForInvalid
      Row 5: last=orphan  + regen=null  → UPDATE old discarded + INSERT human (active)
      Row 6: last=orphan  + regen=hid   → reuse orphan (content must be "")
      Row 7: last=orphan  + regen=!hid  → 400 RegenerateForInvalid

    Gate A closing argument (applies to rows 5-7):
      "Last active message" is defined as SELECT ... WHERE status='active'
      ORDER BY created_at DESC, id DESC LIMIT 1 — it is always the latest active row.
      A "non-orphan human" would require an active AI row strictly after it,
      which would itself be the latest active row — contradicting the definition.
      Therefore "last active row is human" ⟺ "orphan human"; no second query needed.
      Rows 8/9 (non-orphan human paths) are unreachable — raise AssertionError
      if ever reached, to catch future state-space regressions.

    T5 single-write-point (Step 8b): graph stream ends → persist_ai_turn
    writes exactly one ai active row (status='active', role='ai') with the
    accumulated content and finish_reason from the last graph chunk;
    sessions.last_active_at is updated.  No ai row is written on the error path.

    SSE 7-event sequence (M6, §8.1):
      session_meta → [thinking_start → thinking_end] → delta×N → end
    error path: emit error frame (human active row retained, no ai row).

    Lock-release contract: session lock released in generator finally block
    on success; explicit release before StreamingResponse for HTTPException
    paths raised before generator construction (P0-2).

    Stop / user_stop_requested detection is NOT implemented here (Step 8c).
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
                raise AssertionError("unreachable: non-orphan human cannot be last active row")

        await db.commit()

        # ---- streaming response ----
        async def generator() -> AsyncGenerator[bytes, None]:
            accumulated = ""
            last_finish_reason = "stop"  # 兜底；末帧 chunk.response_metadata 命中时覆盖

            # Build initial state for main_graph (same shape as stream_chat dev path)
            from langchain_core.messages import HumanMessage

            from app.chat.state import MainDialogueState

            initial_state: MainDialogueState = {
                "session_id": str(sid),
                "child_user_id": str(current.id),
                "child_profile": None,  # M6: not read by nodes
                "messages": [HumanMessage(content=req.content)],
                "audit_state": {},  # M6: all-False stub
                "pending_guidance": None,
                "generated_token_count": 0,
                "client_alive": True,
                "user_stop_requested": False,
            }

            try:
                yield _frame_sse_event("session_meta", {"session_id": str(sid), "hid": str(hid)})

                # Consume graph stream: accumulate content + forward to SSE
                async for payload in main_graph.astream(initial_state, stream_mode="custom"):
                    d = payload.get("delta", "")
                    if d:
                        accumulated += d
                    fr = payload.get("finish_reason")
                    if fr:
                        last_finish_reason = fr  # stop / length / content_filter

                    # stream_graph_to_sse is an async generator expecting an async iterable
                    # of dict payloads; wrap in a simple async iterator
                    async def _payloads():
                        yield payload
                    async for frame in stream_graph_to_sse(_payloads()):
                        yield frame

                # T5 唯一写入点: INSERT ai active + finish_reason (真实值) + content
                # + UPDATE sessions.last_active_at；graph 内不再有 persist_turn 节点
                aid = await persist_ai_turn(
                    db, sid, finish_reason=last_finish_reason, content=accumulated
                )
                # commit②: persist AI row + session.last_active_at to PG
                await db.commit()
                yield _frame_sse_event(
                    "end",
                    {"finish_reason": last_finish_reason, "aid": str(aid)},
                )
            except Exception as e:
                yield _frame_sse_event("error", {"message": str(e), "code": "InternalError"})
                # human active 行已由上方 db.commit() 持久化，不写 ai 行（不回滚）
            finally:
                await release_session_lock(redis, str(sid), nonce)

        return StreamingResponse(generator(), media_type="text/event-stream")

    except HTTPException:
        await release_session_lock(redis, str(sid), nonce)
        raise
