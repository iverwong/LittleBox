"""Tests for context helper functions (to_lc_message, load_recent_messages).

关注点覆盖（5 条硬约束 + 1 条 session_notes 护栏）：

本文件主要测试：
- to_lc_message：role 到 LangChain 类型的转换
- load_recent_messages：discarded 过滤、turn 范围、as_orm 模式

所有测试使用 conftest.py 的 PostgreSQL test-db fixtures（savepoint 隔离）。
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.core.enums import MessageRole, MessageStatus, UserRole
from app.domain.accounts.models import Family, User
from app.domain.chat.context import (
    load_recent_messages,
    to_lc_message,
)
from app.domain.chat.models import Message, Session


async def _seed_family_and_child(db_session) -> uuid.UUID:
    """Create a family + child user row, return child_user_id."""
    fam = Family()
    db_session.add(fam)
    await db_session.flush()
    uid = uuid.uuid4()
    u = User(id=uid, family_id=fam.id, role=UserRole.child, phone="ctx-test", is_active=True)
    db_session.add(u)
    await db_session.flush()
    return uid


async def _seed_session(db_session, child_user_id: uuid.UUID) -> uuid.UUID:
    """Create a session row (Message needs session_id FK)."""
    sid = uuid.uuid4()
    s = Session(id=sid, child_user_id=child_user_id, title="test")
    db_session.add(s)
    await db_session.flush()
    return sid


# ---- to_lc_message unit ----


def test_to_lc_message_human() -> None:
    """to_lc_message: role=human → HumanMessage"""

    class FakeRow:
        role = MessageRole.human
        content = "hello"

    msg = to_lc_message(FakeRow())
    assert msg.__class__.__name__ == "HumanMessage"
    assert msg.content == "hello"


def test_to_lc_message_ai() -> None:
    """to_lc_message: role=ai → AIMessage"""

    class FakeRow:
        role = MessageRole.ai
        content = "assistant reply"

    msg = to_lc_message(FakeRow())
    assert msg.__class__.__name__ == "AIMessage"
    assert msg.content == "assistant reply"


# ---- load_recent_messages integration ----


@pytest.mark.asyncio
async def test_load_recent_messages_filters_discarded(db_session) -> None:
    """Filter: status='discarded' rows are excluded from results."""
    child_id = await _seed_family_and_child(db_session)
    sid = await _seed_session(db_session, child_id)
    now = datetime.now(timezone.utc)

    # active messages
    db_session.add(
        Message(
            session_id=sid, role=MessageRole.human, content="Human active",
            status=MessageStatus.active, turn_number=1, created_at=now,
        )
    )
    db_session.add(
        Message(
            session_id=sid, role=MessageRole.ai, content="AI active",
            status=MessageStatus.active, turn_number=1, created_at=now + timedelta(seconds=1),
        )
    )
    # discarded message
    db_session.add(
        Message(
            session_id=sid, role=MessageRole.human, content="Human discarded",
            status=MessageStatus.discarded, turn_number=2, created_at=now + timedelta(seconds=2),
        )
    )
    await db_session.flush()

    result = await load_recent_messages(sid, db_session, from_turn=1, to_turn=2)
    assert len(result) == 2
    contents = [m.content for m in result]
    assert "Human discarded" not in contents
    assert "Human active" in contents
    assert "AI active" in contents


@pytest.mark.asyncio
async def test_load_recent_messages_turn_range(db_session) -> None:
    """Range: from_turn / to_turn is correctly enforced."""
    child_id = await _seed_family_and_child(db_session)
    sid = await _seed_session(db_session, child_id)
    now = datetime.now(timezone.utc)

    for tn in range(1, 5):
        db_session.add(
            Message(
                session_id=sid, role=MessageRole.human, content=f"Turn {tn}",
                status=MessageStatus.active, turn_number=tn,
                created_at=now + timedelta(hours=tn),
            )
        )
    await db_session.flush()

    # Query turns 2–3
    result = await load_recent_messages(sid, db_session, from_turn=2, to_turn=3)
    assert len(result) == 2
    assert {m.content for m in result} == {"Turn 2", "Turn 3"}


@pytest.mark.asyncio
async def test_load_recent_messages_as_orm(db_session) -> None:
    """as_orm=True returns Message objects, not BaseMessage."""
    child_id = await _seed_family_and_child(db_session)
    sid = await _seed_session(db_session, child_id)
    now = datetime.now(timezone.utc)

    db_session.add(
        Message(
            session_id=sid, role=MessageRole.human, content="Hello",
            status=MessageStatus.active, turn_number=1, created_at=now,
        )
    )
    await db_session.flush()

    result = await load_recent_messages(sid, db_session, from_turn=1, to_turn=1, as_orm=True)
    assert len(result) == 1
    assert isinstance(result[0], Message)
    assert result[0].content == "Hello"


@pytest.mark.asyncio
async def test_load_recent_messages_empty_when_all_discarded(db_session) -> None:
    """Filter: when all messages in range are discarded, result is empty."""
    child_id = await _seed_family_and_child(db_session)
    sid = await _seed_session(db_session, child_id)
    now = datetime.now(timezone.utc)

    db_session.add(
        Message(
            session_id=sid, role=MessageRole.human, content="Gone",
            status=MessageStatus.discarded, turn_number=1, created_at=now,
        )
    )
    await db_session.flush()

    result = await load_recent_messages(sid, db_session, from_turn=1, to_turn=1)
    assert len(result) == 0
