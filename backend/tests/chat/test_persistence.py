"""Tests for persist_ai_turn and enqueue_audit helpers.

M6 Step 6 — these helpers are TOP-LEVEL EXPORTS from graph.py,
called from me.py generator (Step 8b T5 single-write-point).
They are NOT called from inside the graph.

Coverage:
- persist_ai_turn: writes ai active row + finish_reason + content;
  updates sessions.last_active_at
- enqueue_audit: M6 stub no-op + logger.warning
"""

import logging
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

from app.chat.graph import enqueue_audit, persist_ai_turn
from app.models.chat import Message, Session
from app.models.enums import InterventionType, MessageRole, MessageStatus


def _msg_by_session(db_session, sid):
    return (
        select(Message)
        .where(Message.session_id == sid)
        .order_by(Message.created_at)
    )


def _session_last_active(db_session, sid):
    return (
        select(Session.last_active_at)
        .where(Session.id == sid)
    )


# ---------------------------------------------------------------------------
# persist_ai_turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_ai_turn_inserts_active_ai_message(db_session, child_user):
    """persist_ai_turn creates a status='active', role='ai' message row and returns its id."""
    sid = uuid.uuid4()
    session = Session(id=sid, child_user_id=child_user.id, title="test")
    db_session.add(session)
    await db_session.flush()

    returned_id = await persist_ai_turn(
        db_session,
        sid=sid,
        finish_reason="stop",
        content="Hello, world!",
    )
    await db_session.flush()

    msg = (await db_session.execute(_msg_by_session(db_session, sid))).scalar_one_or_none()

    assert msg is not None
    assert msg.role == MessageRole.ai
    assert msg.status == MessageStatus.active
    assert msg.content == "Hello, world!"
    assert msg.finish_reason == "stop"
    assert returned_id == msg.id  # F2: returns uuid.UUID of inserted row


@pytest.mark.asyncio
async def test_persist_ai_turn_no_longer_updates_last_active_at(db_session, child_user):
    """M6-patch3: persist_ai_turn no longer updates sessions.last_active_at（F 决策：commit① 独占）。"""
    sid = uuid.uuid4()
    session = Session(id=sid, child_user_id=child_user.id, title="test")
    db_session.add(session)
    await db_session.flush()

    old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
    await db_session.execute(
        update(Session).where(Session.id == sid).values(last_active_at=old_time)
    )
    await db_session.flush()

    await persist_ai_turn(
        db_session,
        sid=sid,
        finish_reason="stop",
        content="reply",
    )
    await db_session.flush()

    unchanged = (await db_session.execute(_session_last_active(db_session, sid))).scalar_one()

    assert unchanged == old_time  # persist_ai_turn 不再覆写 last_active_at


@pytest.mark.asyncio
async def test_persist_ai_turn_accepts_intervention_type(db_session, child_user):
    """persist_ai_turn writes intervention_type=crisis correctly."""
    sid = uuid.uuid4()
    session = Session(id=sid, child_user_id=child_user.id, title="test")
    db_session.add(session)
    await db_session.flush()

    await persist_ai_turn(
        db_session,
        sid=sid,
        finish_reason="stop",
        content="crisis response",
        intervention_type=InterventionType.crisis,
    )
    await db_session.flush()

    msg = (await db_session.execute(_msg_by_session(db_session, sid))).scalar_one()

    assert msg.intervention_type == InterventionType.crisis


# ---------------------------------------------------------------------------
# ai_turn_counter increment (M8 Step 8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ai_turn_counter_increments_after_each_turn(db_session, child_user):
    """连续 3 轮后 counter == 3。"""
    sid = uuid.uuid4()
    session = Session(id=sid, child_user_id=child_user.id, title="test")
    db_session.add(session)
    await db_session.flush()

    for i in range(3):
        await persist_ai_turn(db_session, sid=sid, finish_reason="stop", content=f"reply{i}")
        await db_session.flush()

    row = await db_session.execute(
        select(Session.ai_turn_counter).where(Session.id == sid)
    )
    assert row.scalar_one() == 3


@pytest.mark.asyncio
async def test_ai_turn_counter_starts_at_zero(db_session, child_user):
    """新建 session 的 ai_turn_counter 默认值为 0。"""
    sid = uuid.uuid4()
    session = Session(id=sid, child_user_id=child_user.id, title="test")
    db_session.add(session)
    await db_session.flush()

    row = await db_session.execute(
        select(Session.ai_turn_counter).where(Session.id == sid)
    )
    assert row.scalar_one() == 0


# 并发行锁测试在此文件省略：PG 行锁是数据库保证，
# _session_maker() 违反测试隔离纪律（conftest 黑名单）。
# 顺序调用 3 轮 counter==3 已覆盖增量语义。并发安全由 PG 行锁保障。


# ---------------------------------------------------------------------------
# enqueue_audit — M6 stub
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_audit_m6_stub_is_noop(db_session, child_user, caplog):
    """M6: enqueue_audit does nothing; logs a warning."""
    sid = uuid.uuid4()
    session = Session(id=sid, child_user_id=child_user.id, title="test")
    db_session.add(session)
    await db_session.flush()

    with caplog.at_level(logging.WARNING):
        await enqueue_audit(sid, db_session)

    # Must have logged the M6 stub warning
    assert any(
        "M6 stub" in msg and "enqueue_audit" in msg
        for msg in caplog.messages
    ), f"Expected M6 stub warning, got: {caplog.messages}"
