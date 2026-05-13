"""Request/response schemas for /me/sessions endpoints (Step 7) and chat stream (Step 8a)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SessionListItem(BaseModel):
    """One session in GET /me/sessions."""

    id: uuid.UUID
    title: str | None
    last_active_at: datetime


class SessionListResponse(BaseModel):
    """GET /me/sessions response."""

    sessions: list[SessionListItem]
    today_session_id: uuid.UUID | None = None
    next_cursor: str | None


class MessageListItem(BaseModel):
    """One message in GET /me/sessions/{id}/messages."""

    id: uuid.UUID
    role: Literal["human", "ai"]
    content: str
    status: Literal["active", "discarded"]
    finish_reason: str | None
    created_at: datetime


class MessageListResponse(BaseModel):
    """GET /me/sessions/{id}/messages response."""

    items: list[MessageListItem]
    next_cursor: str | None
    in_progress: bool


class ChatStreamRequest(BaseModel):
    """POST /me/chat/stream request body.

    Args:
        content: The user's message text. May be empty string when
            ``regenerate_for`` points to an orphan human (regenerating the
            same question without changing content).
        session_id: null for first turn; existing session UUID for
            subsequent turns.
        regenerate_for: Must be the id of the session's last active human
            message.  Passing any other value (earlier human / any ai /
            non-existent row) returns 400 ``RegenerateForInvalid``.
    """

    content: str = Field(
        description="User message text. Empty allowed only for row-7 regeneration."
    )
    session_id: str | None = Field(
        default=None,
        description=(
            "前端 hint；服务端按日切策略强制重判生效 sid，"
            "不一致时通过 SSE session_meta.session_id 回灌生效 sid。"
            "null = first turn；非 null = existing session id hint。"
        ),
    )
    regenerate_for: str | None = Field(
        default=None,
        description=(
            "Must be the id of the session's current last active human message. "
            "Pointing to an earlier human / to an ai / to a non-existent row"
            " → 400 RegenerateForInvalid."
        ),
    )
