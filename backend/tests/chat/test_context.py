"""Tests for context helper functions (to_lc_message, load_recent_messages).

关注点覆盖（5 条硬约束 + 1 条 session_notes 护栏）：

本文件主要测试：
- load_recent_messages
- to_lc_message

所有测试使用 conftest.py 的 PostgreSQL test-db fixtures（savepoint 隔离）。
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.domain.chat.context import (
    to_lc_message,
    load_recent_messages,
)
from app.core.enums import MessageRole, MessageStatus
from app.domain.audit.models import RollingSummary
from app.domain.chat.models import Message, Session
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


async def _seed_session(db_session, child_user_id: uuid.UUID) -> uuid.UUID:
    """Create a session row (Message needs session_id FK)."""
    sid = uuid.uuid4()
    s = Session(id=sid, child_user_id=child_user_id, title="test")
    db_session.add(s)
    await db_session.flush()
    return sid


# ---- _to_lc_message unit ----


def test_to_lc_message_human() -> None:
    """_to_lc_message: role=human → HumanMessage"""

    class FakeRow:
        role = MessageRole.human
        content = "hello"

    msg = to_lc_message(FakeRow())
    assert msg.__class__.__name__ == "HumanMessage"
    assert msg.content == "hello"


def test_to_lc_message_ai() -> None:
    """_to_lc_message: role=ai → AIMessage"""

    class FakeRow:
        role = MessageRole.ai
        content = "assistant reply"

    msg = to_lc_message(FakeRow())
    assert msg.__class__.__name__ == "AIMessage"
    assert msg.content == "assistant reply"
