"""M8 端到端烟雾测试：enqueue_audit → pending → run_audit → ready 闭环验证。

B.1a 范围：仅验 Redis 信号闭环 pending→ready，不验 audit_records 落库
（由 test_writers.py 单独覆盖）。
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fakeredis.aioredis import FakeRedis

from app.chat.graph import enqueue_audit
from app.audit.worker import run_audit
from app.state.audit_signals import AuditSignalsManager
from app.models.accounts import Family, FamilyMember, User
from app.models.enums import UserRole
from app.models.chat import Session as SessionModel
from app.schemas.audit import AuditDimensionScores, AuditOutputSchema

pytestmark = pytest.mark.audit

_AUDIT_OUT = AuditOutputSchema(
    dimension_scores=AuditDimensionScores(),
    crisis_detected=False, crisis_topic=None,
    redline_triggered=False, redline_detail=None,
    guidance="ok", turn_summary="ok",
)


@pytest.mark.asyncio
async def test_e2e_enqueue_to_ready(concurrent_db_sessions):
    """enqueue → pending → run_audit → ready 闭环验证。"""
    sessions = await concurrent_db_sessions(
        count=1,
        tables=[
            "messages", "sessions", "users", "family_members",
            "families", "audit_records", "rolling_summaries",
        ],
    )
    db = sessions[0]

    # setup: family + user + session row
    fam = Family()
    db.add(fam)
    await db.flush()
    child = User(
        family_id=fam.id, role=UserRole.child,
        phone="e2e-test", is_active=True,
    )
    db.add(child)
    await db.flush()
    db.add(FamilyMember(
        family_id=fam.id, user_id=child.id, role=UserRole.child,
    ))
    await db.flush()
    session = SessionModel(id=uuid.uuid4(), child_user_id=child.id, title="test")
    db.add(session)
    await db.commit()
    sid = session.id

    shared_redis = FakeRedis(decode_responses=True)
    real_manager = AuditSignalsManager(shared_redis, ttl=86400)
    mock_arq = AsyncMock()

    # 1) enqueue_audit → pending
    # 注：不 mock AuditSignalsManager，enqueue_audit 内部用 shared_redis
    # 构造的 manager 与 real_manager 共享同一 FakeRedis 数据
    with (
        patch("redis.asyncio.Redis.from_url", return_value=shared_redis),
        patch("arq.create_pool", return_value=mock_arq),
    ):
        await enqueue_audit(sid, db, turn_number=1, child_user_id=child.id)

    payload = await real_manager.get(str(sid))
    assert payload is not None
    assert payload.status == "pending"
    assert payload.turn == 1

    # 2) run_audit → ready
    # T10：构造 fake RuntimeResources + ctx，通过 audit_graph.ainvoke 走通
    from unittest.mock import MagicMock
    from app.runtime import RuntimeResources
    fake_rr = MagicMock(spec=RuntimeResources)
    fake_graph = AsyncMock()
    fake_graph.ainvoke = AsyncMock(return_value={"structured_output": _AUDIT_OUT})
    fake_rr.audit_graph = fake_graph
    fake_rr.settings = MagicMock()
    fake_rr.settings.max_audit_tool_iterations = 5
    fake_rr.audit_redis = shared_redis
    fake_rr.db_session_factory = MagicMock()

    worker_ctx = {
        "redis": shared_redis,
        "job_try": 1,
        "resources": fake_rr,
        "signals_manager": AuditSignalsManager(shared_redis, ttl=86400),
    }
    await run_audit(worker_ctx, str(sid), 1, str(child.id))

    payload = await real_manager.get(str(sid))
    assert payload is not None
    assert payload.status == "ready"
