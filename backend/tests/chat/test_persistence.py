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
from sqlalchemy.ext.asyncio import AsyncSession

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


class TestSqlExpressionGuard:
    """静态防御：persist_ai_turn 必须用 SQL 列表达式自增 ai_turn_counter。

    M8-hotfix-2 偏差闭环。禁止退化为 Python 端读改写（+= / -= / old + 1）。
    """

    def test_persist_ai_turn_uses_sql_expression(self) -> None:
        import inspect
        import re
        from app.chat import graph

        source = inspect.getsource(graph.persist_ai_turn)

        assert re.search(r"Session\.ai_turn_counter\s*\+\s*1", source), (
            "persist_ai_turn 必须用 SQL 列表达式自增 ai_turn_counter"
        )
        assert not re.search(r"\.ai_turn_counter\s*[+\-]=", source), (
            "禁止 Python 复合赋值修改 ai_turn_counter（会引入 read-modify-write 竞态）"
        )
        graph_source = inspect.getsource(graph)
        assert "from sqlalchemy import update" in graph_source, (
            "graph.py 必须 import sqlalchemy.update"
        )


class TestConcurrentRowLock:
    """M8-hotfix-2 偏差闭环：真并发验证 PG 行锁 + SQL 列表达式原子性。"""

    @pytest.mark.asyncio
    async def test_persist_ai_turn_concurrent_row_lock(
        self, concurrent_db_sessions, engine,
    ) -> None:
        import asyncio
        from app.models.accounts import Family, FamilyMember, User
        from app.models.enums import UserRole

        sessions = await concurrent_db_sessions(
            count=6,
            tables=["messages", "sessions", "users", "family_members", "families"],
        )
        setup_db, *worker_dbs = sessions

        fam = Family()
        setup_db.add(fam)
        await setup_db.flush()
        child = User(
            family_id=fam.id, role=UserRole.child,
            phone="conc-13800", is_active=True,
        )
        setup_db.add(child)
        await setup_db.flush()
        setup_db.add(FamilyMember(
            family_id=fam.id, user_id=child.id, role=UserRole.child,
        ))
        await setup_db.flush()
        session_row = Session(id=uuid.uuid4(), child_user_id=child.id, title="test")
        setup_db.add(session_row)
        await setup_db.commit()
        sid = session_row.id

        async def _one_turn(db):
            await persist_ai_turn(
                db, sid, finish_reason="stop", content="concurrent test turn",
            )
            await db.commit()

        await asyncio.gather(*[_one_turn(db) for db in worker_dbs])

        async with AsyncSession(engine) as check:
            result = await check.execute(
                select(Session.ai_turn_counter).where(Session.id == sid)
            )
            counter = result.scalar_one()
            assert counter == 5, f"PG 行锁失败：5 并发后 counter={counter}"
            msg_count = await check.execute(
                select(Message).where(
                    Message.session_id == sid, Message.role == MessageRole.ai,
                )
            )
            assert len(msg_count.scalars().all()) == 5


# ---------------------------------------------------------------------------
# enqueue_audit (M8 Step 9: Redis SET pending + ARQ enqueue)
# ---------------------------------------------------------------------------


@pytest.mark.audit
@pytest.mark.asyncio
async def test_enqueue_audit_sets_pending_and_enqueues(db_session, child_user):
    """enqueue_audit 完成 Redis SET pending + ARQ enqueue_job。"""
    from unittest.mock import AsyncMock, patch

    from app.audit.worker import run_audit

    sid = uuid.uuid4()
    session = Session(id=sid, child_user_id=child_user.id, title="test")
    db_session.add(session)
    await db_session.flush()

    mock_arq_pool = AsyncMock()
    mock_arq_pool.enqueue_job = AsyncMock()
    mock_arq_pool.close = AsyncMock()
    mock_arq_pool.connection_pool.disconnect = AsyncMock()

    mock_manager = AsyncMock()
    mock_manager.set_pending = AsyncMock()

    from unittest.mock import MagicMock
    from redis.asyncio import Redis as _Redis

    mock_redis = AsyncMock(spec=_Redis)
    mock_redis.connection_pool = MagicMock()
    mock_redis.connection_pool.connection_kwargs = {
        "host": "localhost", "port": 6379, "password": None,
    }

    with (
        patch("redis.asyncio.Redis.from_url", return_value=mock_redis),
        patch(
            "app.chat.graph.AuditSignalsManager",
            return_value=mock_manager,
        ),
        patch(
            "arq.create_pool",
            return_value=mock_arq_pool,
        ),
    ):
        await enqueue_audit(sid, db_session, turn_number=1, child_user_id=child_user.id, target_message_id=sid)

        mock_manager.set_pending.assert_awaited_once()
        args, kwargs = mock_manager.set_pending.await_args
        assert args[:2] == (str(sid), 1)
        assert "started_at" in kwargs
        started_at = kwargs["started_at"]
        assert started_at.endswith("+00:00") or started_at.endswith("Z"), (
            f"started_at must be UTC, got {started_at!r}"
        )
        mock_arq_pool.enqueue_job.assert_awaited_once()
