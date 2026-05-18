"""DB 写入路径测试：audit_records INSERT + rolling_summaries upsert。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.audit.writers import write_audit_results
from app.models.accounts import Family, User
from app.models.enums import UserRole
from app.schemas.audit import (
    AuditDimensionScores,
    AuditOutputSchema,
)

pytestmark = pytest.mark.asyncio


_BASE_OUTPUT = AuditOutputSchema(
    dimension_scores=AuditDimensionScores(
        emotional=5, social=3, romance=0, values=2,
        boundaries=1, academic=4, lifestyle=0,
    ),
    crisis_detected=False,
    crisis_topic=None,
    redline_triggered=False,
    redline_detail=None,
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
    """5 场景覆盖。"""

    async def test_first_insert(self, db_session, sid):
        """首次写入：audit_records INSERT + rolling_summaries INSERT。"""
        await write_audit_results(
            db_session, str(sid), turn_number=1,
            structured_output=_BASE_OUTPUT,
            session_notes_final="用户今天情绪稳定。",
            turn_summary="情绪稳定",
        )

        # 验证 audit_records
        ar = (
            await db_session.execute(
                text("SELECT turn_number, crisis_detected, guidance_injection FROM audit_records WHERE session_id=:sid"),
                {"sid": sid},
            )
        ).fetchone()
        assert ar is not None
        assert ar.turn_number == 1
        assert ar.crisis_detected is False
        assert ar.guidance_injection == "观察社交互动"

        # 验证 rolling_summaries
        rs = (
            await db_session.execute(
                text("SELECT last_turn, crisis_locked, session_notes, turn_summaries FROM rolling_summaries WHERE session_id=:sid"),
                {"sid": sid},
            )
        ).fetchone()
        assert rs is not None
        assert rs.last_turn == 1
        assert rs.crisis_locked is False
        assert rs.session_notes == "用户今天情绪稳定。"
        assert len(rs.turn_summaries) == 1
        assert rs.turn_summaries[0]["turn_number"] == 1

    async def test_second_update(self, db_session, sid):
        """第二轮写入：UPDATE rolling_summaries（append turn_summary）。"""
        output_2 = _BASE_OUTPUT.model_copy(update={"guidance": "继续观察", "turn_summary": "社交增多"})

        await write_audit_results(db_session, str(sid), 1, _BASE_OUTPUT, "第一轮笔记", "第一轮摘要")
        await write_audit_results(db_session, str(sid), 2, output_2, "第二轮笔记", "第二轮摘要")

        rs = (
            await db_session.execute(
                text("SELECT last_turn, session_notes, turn_summaries FROM rolling_summaries WHERE session_id=:sid"),
                {"sid": sid},
            )
        ).fetchone()
        assert rs.last_turn == 2
        assert rs.session_notes == "第二轮笔记"  # 被覆盖
        assert len(rs.turn_summaries) == 2
        assert rs.turn_summaries[1]["turn_number"] == 2
        assert rs.turn_summaries[1]["summary"] == "第二轮摘要"

    async def test_turn_rollback_rejected(self, db_session, sid):
        """旧 turn 写入不应覆盖（WHERE last_turn < :turn 防回退）。"""
        await write_audit_results(db_session, str(sid), 5, _BASE_OUTPUT, "第5轮", "摘要5")
        await write_audit_results(db_session, str(sid), 3, _BASE_OUTPUT, "不应出现", "摘要3")

        rs = (
            await db_session.execute(
                text("SELECT last_turn, session_notes FROM rolling_summaries WHERE session_id=:sid"),
                {"sid": sid},
            )
        ).fetchone()
        assert rs.last_turn == 5  # 未被覆盖
        assert rs.session_notes == "第5轮"  # 未被覆盖

    async def test_crisis_locked_accumulation(self, db_session, sid):
        """crisis_locked 一旦为 true 不可被 false 回退。"""
        crisis_out = _BASE_OUTPUT.model_copy(update={"crisis_detected": True, "crisis_topic": "自残倾向"})

        # 第一轮：crisis=false
        await write_audit_results(db_session, str(sid), 1, _BASE_OUTPUT, "第1轮", "摘要1")
        # 第二轮：crisis=true
        await write_audit_results(db_session, str(sid), 2, crisis_out, "第2轮", "摘要2")
        # 第三轮：crisis=false，尝试覆盖
        await write_audit_results(db_session, str(sid), 3, _BASE_OUTPUT, "第3轮", "摘要3")

        rs = (
            await db_session.execute(
                text("SELECT crisis_locked FROM rolling_summaries WHERE session_id=:sid"),
                {"sid": sid},
            )
        ).fetchone()
        assert rs.crisis_locked is True  # true 不可回 false

    async def test_audit_records_multiple_turns(self, db_session, sid):
        """多次写入验证：同 session 多轮 audit_records 正确累积。"""
        for t in range(1, 4):
            out = _BASE_OUTPUT.model_copy(update={"guidance": f"第{t}轮"})
            await write_audit_results(db_session, str(sid), t, out, f"笔记{t}", f"摘要{t}")

        rows = (
            await db_session.execute(
                text("SELECT turn_number, guidance_injection FROM audit_records WHERE session_id=:sid ORDER BY turn_number"),
                {"sid": sid},
            )
        ).fetchall()
        assert len(rows) == 3
        assert [r.turn_number for r in rows] == [1, 2, 3]
        assert [r.guidance_injection for r in rows] == ["第1轮", "第2轮", "第3轮"]

        rs = (
            await db_session.execute(
                text("SELECT last_turn, session_notes FROM rolling_summaries WHERE session_id=:sid"),
                {"sid": sid},
            )
        ).fetchone()
        assert rs.last_turn == 3
        assert rs.session_notes == "笔记3"
