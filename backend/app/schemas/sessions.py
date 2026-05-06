"""Response schemas for /me/sessions endpoints (Step 7)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class SessionListItem(BaseModel):
    """One session in GET /me/sessions."""

    id: uuid.UUID
    title: str | None
    last_active_at: datetime


class SessionListResponse(BaseModel):
    """GET /me/sessions response."""

    items: list[SessionListItem]
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
