"""DB 写入路径测试：audit_records INSERT + rolling_summaries upsert（6 条 + crisis_locked_message_id 短路）。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.audit.writers import write_audit_results
from app.models.accounts import Family, User
from app.models.audit import RollingSummary
from app.models.enums import UserRole
from app.schemas.audit import (
    AuditDimensionScores,
    AuditOutputSchema,
)

pytestmark = [
    pytest.mark.audit,
    pytest.mark.asyncio,
]


_BASE_OUTPUT = AuditOutputSchema(
    dimension_scores=AuditDimensionScores(
        emotional=5, social=3, romance=0, values=2,
        boundaries=1, academic=4, lifestyle=0,
    ),
    crisis_detected=False, crisis_topic=None,
    redline_triggered=False, redline_detail=None,
    guidance="观察社交互动",
    turn_summary="社交活跃",
)


@pytest_asyncio.fixture
async def sid(db_session) -> uuid.UUID:
    """种子一个 child user + session，返回 session_id。"""
    session_id = uuid.uuid4()
    child_id = uuid.uuid4()
    fam = Family()
    db_session.add(fam)
    await db_session.flush()
    child = User(id=child_id, family_id=fam.id, role=UserRole.child, phone="wa", is_active=True)
    db_session.add(child)
    await db_session.flush()
    await db_session.execute(
        text("""
            INSERT INTO sessions (id, child_user_id, status, last_active_at, created_at, needs_compression)
            VALUES (:id, :child_id, 'active', :now, :now, false)
        """),
        {"id": session_id, "child_id": child_id, "now": datetime.now(timezone.utc)},
    )
    await db_session.commit()
    return session_id


class TestWriteAuditResults:
    """5 业务路径 + 1 D13 原子性 = 6 条。"""

    async def test_first_insert(self, db_session, sid):
        """首次写入：audit_records INSERT + rolling_summaries INSERT。"""
        await write_audit_results(
            db_session, str(sid), 1, _BASE_OUTPUT,
            "用户今天情绪稳定。", "情绪稳定",
        )
        await db_session.flush()  # 确保 ORM pending 落盘后查询

        ar = (
            await db_session.execute(
                text("SELECT turn_number, crisis_detected, guidance_injection, notify_sent "
                     "FROM audit_records WHERE session_id=:sid"),
                {"sid": sid},
            )
        ).fetchone()
        assert ar is not None
        assert ar.turn_number == 1
        assert ar.crisis_detected is False
        assert ar.guidance_injection == "观察社交互动"
        assert ar.notify_sent is False

        rs = (
            await db_session.execute(
                text("SELECT last_turn, crisis_locked_message_id, session_notes FROM rolling_summaries WHERE session_id=:sid"),
                {"sid": sid},
            )
        ).fetchone()
        assert rs is not None
        assert rs.last_turn == 1
        assert rs.crisis_locked_message_id is None
        assert rs.session_notes == "用户今天情绪稳定。"

    async def test_second_update(self, db_session, sid):
        """第二轮写入：UPDATE rolling_summaries（append turn_summary）。"""
        output_2 = _BASE_OUTPUT.model_copy(update={"guidance": "继续观察", "turn_summary": "社交增多"})

        await write_audit_results(db_session, str(sid), 1, _BASE_OUTPUT, "第一轮笔记", "第一轮摘要")
        await db_session.flush()
        await write_audit_results(db_session, str(sid), 2, output_2, "第二轮笔记", "第二轮摘要")
        await db_session.flush()

        rs = (
            await db_session.execute(
                text("SELECT last_turn, session_notes, turn_summaries FROM rolling_summaries WHERE session_id=:sid"),
                {"sid": sid},
            )
        ).fetchone()
        assert rs.last_turn == 2
        assert rs.session_notes == "第二轮笔记"
        summaries = rs.turn_summaries
        assert len(summaries) == 2
        assert summaries[1]["turn_number"] == 2
        assert summaries[1]["summary"] == "第二轮摘要"

    async def test_turn_rollback_rejected(self, db_session, sid, caplog):
        """回退防御：turn=5 写入后 turn=3 → WARN + 零净写入（连 audit_records 也不插）。"""
        await write_audit_results(db_session, str(sid), 5, _BASE_OUTPUT, "第5轮", "摘要5")
        await db_session.flush()

        with caplog.at_level("WARNING", logger="audit.db"):
            await write_audit_results(db_session, str(sid), 3, _BASE_OUTPUT, "不应出现", "摘要3")

        assert "audit.turn.rollback" in caplog.text

        rs = (await db_session.execute(
            text("SELECT last_turn FROM rolling_summaries WHERE session_id=:sid"),
            {"sid": sid},
        )).fetchone()
        assert rs.last_turn == 5  # 未被覆盖

        count = (await db_session.execute(
            text("SELECT COUNT(*) FROM audit_records WHERE session_id=:sid"),
            {"sid": sid},
        )).scalar()
        assert count == 1  # 仅 turn=5, 无 turn=3

    async def test_crisis_locked_short_circuit(self, db_session, sid):
        """crisis_locked_message_id 短路保留：命中后非空；后续轮 crisis_detected=False 但旧值保留。"""
        crisis_out = _BASE_OUTPUT.model_copy(
            update={"crisis_detected": True, "crisis_topic": "自残倾向"},
        )
        target_id = uuid.uuid4()

        await write_audit_results(db_session, str(sid), 1, _BASE_OUTPUT, "第1轮", "摘要1",
                                  target_message_id=target_id)
        await db_session.flush()
        await write_audit_results(db_session, str(sid), 2, crisis_out, "第2轮", "摘要2",
                                  target_message_id=target_id)
        await db_session.flush()
        await write_audit_results(db_session, str(sid), 3, _BASE_OUTPUT, "第3轮", "摘要3",
                                  target_message_id=target_id)

        rs = (await db_session.execute(
            text("SELECT crisis_locked_message_id FROM rolling_summaries WHERE session_id=:sid"),
            {"sid": sid},
        )).fetchone()
        # 第 2 轮命中 crisis → crisis_locked_message_id 非空
        assert rs.crisis_locked_message_id is not None
        assert rs.crisis_locked_message_id == target_id

    async def test_multiple_turns(self, db_session, sid):
        """3 轮正确累积 + 无回退告警。"""
        for t in range(1, 4):
            out = _BASE_OUTPUT.model_copy(update={"guidance": f"第{t}轮"})
            await write_audit_results(db_session, str(sid), t, out, f"笔记{t}", f"摘要{t}")
            await db_session.flush()

        ar_rows = (await db_session.execute(
            text("SELECT turn_number, guidance_injection FROM audit_records WHERE session_id=:sid ORDER BY turn_number"),
            {"sid": sid},
        )).fetchall()
        assert len(ar_rows) == 3
        assert [r.turn_number for r in ar_rows] == [1, 2, 3]

        rs = (await db_session.execute(
            text("SELECT last_turn FROM rolling_summaries WHERE session_id=:sid"),
            {"sid": sid},
        )).fetchone()
        assert rs.last_turn == 3

    async def test_atomic_rollback(self, db_session, sid, monkeypatch):
        """D13: upsert 阶段抛异常 → audit_records INSERT 同步回滚。

        通过 monkeypatch db_session.add 使 RollingSummary add 时抛异常，
        模拟 upsert 阶段失败。验证 audit_records 无残留。
        """
        original_add = db_session.add
        add_counter = 0

        def _mock_add(instance):
            nonlocal add_counter
            add_counter += 1
            if add_counter >= 2:  # 第二次 add（RollingSummary）→ 模拟 upsert 失败
                raise ValueError("模拟 upsert 阶段异常")
            original_add(instance)

        monkeypatch.setattr(db_session, "add", _mock_add)

        with pytest.raises(ValueError):
            await write_audit_results(
                db_session, str(sid), 1, _BASE_OUTPUT, "笔记", "摘要",
            )
        # 异常后 session 含 pending 对象，需回滚以丢弃
        await db_session.rollback()

        count = (await db_session.execute(
            text("SELECT COUNT(*) FROM audit_records WHERE session_id=:sid"),
            {"sid": sid},
        )).scalar()
        assert count == 0, "upsert 失败后 audit_records 应被整体回滚"
