"""Expert 域仓储层只读查询 SQL 正确性测试。

使用 concurrent_db_sessions fixture 实现真 commit + TRUNCATE 清空。
每测试独立 UUID 簇防冲突。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from app.core.enums import DailyStatus
from app.domain.expert.repository import (
    _extract_snippet,
    _match_matched,
    fetch_notes,
    fetch_report,
    fetch_turn,
    search_crisis_topics,
    search_daily_reports,
    search_session_notes,
    search_turn_summaries,
)
from sqlalchemy import text
from tests._tables import _GUARD_TABLES as _TABLES

# 注意：pytest.mark.asyncio 只在需要 async 的类上单独标注

REPORT_DATE = date(2026, 6, 23)
NOW = datetime.now(UTC)


def _make_ids() -> dict:
    """每测试生成独立 UUID 簇。"""
    return {
        "fam_id": f"{uuid.uuid4()}",
        "cid": f"{uuid.uuid4()}",
        "sid1": f"{uuid.uuid4()}",
        "sid2": f"{uuid.uuid4()}",
    }


async def _seed_data(db, ids: dict):
    """种子数据：family + user + 2 sessions + turn_summaries + notes + crisis + reports。"""
    fam_id, cid, sid1, sid2 = ids["fam_id"], ids["cid"], ids["sid1"], ids["sid2"]

    await db.execute(
        text("INSERT INTO families (id) VALUES (:fam_id)"),
        {"fam_id": fam_id},
    )
    await db.execute(
        text("""
            INSERT INTO users (id, family_id, role, is_active)
            VALUES (:cuid, :fam_id, 'child', true)
        """),
        {"cuid": cid, "fam_id": fam_id},
    )
    await db.execute(
        text("""
            INSERT INTO sessions (id, child_user_id, status, created_at)
            VALUES (:sid1, :cuid, 'active', :now),
                   (:sid2, :cuid, 'active', :now)
        """),
        {
            "sid1": sid1,
            "sid2": sid2,
            "cuid": cid,
            "now": NOW - timedelta(days=1),
        },
    )
    for sid, turn in [(sid1, 1), (sid1, 2), (sid2, 1)]:
        await db.execute(
            text("""
                INSERT INTO messages (session_id, role, content, turn_number, status, created_at)
                VALUES (:sid, 'human', :content, :turn, 'active', :now)
            """),
            {
                "sid": sid,
                "turn": turn,
                "content": f"Test message turn {turn}",
                "now": NOW - timedelta(days=1),
            },
        )
    turn_summaries_1 = json.dumps(
        [
            {"turn_number": 1, "summary": "今天在学校玩得很开心"},
            {"turn_number": 2, "summary": "讨论了下周末去哪里玩"},
        ]
    )
    turn_summaries_2 = json.dumps(
        [
            {"turn_number": 1, "summary": "今天有点不开心"},
        ]
    )
    await db.execute(
        text("""
            INSERT INTO rolling_summaries
                (session_id, last_turn, turn_summaries, session_notes, updated_at)
            VALUES (:sid1, 2, CAST(:ts1 AS jsonb), :notes1, :now),
                   (:sid2, 1, CAST(:ts2 AS jsonb), :notes2, :now)
        """),
        {
            "sid1": sid1,
            "sid2": sid2,
            "ts1": turn_summaries_1,
            "ts2": turn_summaries_2,
            "notes1": "孩子今天聊了很多关于学校的事情",
            "notes2": "孩子今天情绪不太稳定",
            "now": NOW - timedelta(days=1),
        },
    )
    await db.execute(
        text("""
            INSERT INTO audit_records
                (session_id, turn_number, crisis_detected, crisis_topic, dimension_scores,
                 guidance_injection, created_at)
            VALUES
                (:sid1, 1, true, '校园社交冲突', '{}'::jsonb, '', :now),
                (:sid1, 2, false, NULL, '{}'::jsonb, '', :now)
        """),
        {
            "sid1": sid1,
            "now": NOW - timedelta(days=1),
        },
    )
    await db.execute(
        text("""
            INSERT INTO daily_reports
                (child_user_id, session_id, report_date, overall_status,
                 today_overview, what_was_discussed, emotion_changes,
                 noteworthy, suggestions, anomaly_periods,
                 created_at)
            VALUES
                (:cuid, :sid1, :rd1, :status1,
                 :ov1, :wd1, :ec1, :nt1, :sg1, :ap1, :now1),
                (:cuid, :sid2, :rd2, :status2,
                 :ov2, :wd2, :ec2, :nt2, :sg2, :ap2, :now2)
        """),
        {
            "cuid": cid,
            "sid1": sid1,
            "sid2": sid2,
            "rd1": REPORT_DATE - timedelta(days=1),
            "rd2": REPORT_DATE - timedelta(days=2),
            "status1": DailyStatus.stable.value,
            "status2": DailyStatus.attention.value,
            "ov1": "平稳的一天",
            "wd1": "玩了游戏",
            "ec1": "情绪稳定",
            "nt1": "无特别",
            "sg1": "继续观察",
            "ap1": "无",
            "ov2": "需要关注",
            "wd2": "情绪波动",
            "ec2": "有些焦虑",
            "nt2": "留意变化",
            "sg2": "多沟通",
            "ap2": "傍晚",
            "now1": NOW - timedelta(days=1),
            "now2": NOW - timedelta(days=2),
        },
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


class TestHelpers:
    """仓储层辅助函数测试。"""

    def test_match_matched(self):
        result = _match_matched("今天在学校玩游戏", ["游戏", "学校", "不存在的"])
        assert result == ["游戏", "学校"]

    def test_match_matched_dedup(self):
        result = _match_matched("游戏，游戏", ["游戏", "游戏"])
        assert result == ["游戏"]

    def test_extract_snippet_context_window(self):
        text_str = "a" * 50 + "关键字" + "b" * 50
        snippet = _extract_snippet(text_str, ["关键字"], context_chars=10)
        assert "关键字" in snippet
        assert snippet.startswith("...")
        assert snippet.endswith("...")

    def test_extract_snippet_no_match(self):
        snippet = _extract_snippet("hello world", ["关键字"], context_chars=10)
        assert snippet == "hello worl"
        assert len(snippet) == 10

    def test_extract_snippet_zero_context(self):
        text_str = "第一行\n有关键字这一行\n第三行"
        snippet = _extract_snippet(text_str, ["关键字"], context_chars=0)
        assert "有关键字这一行" in snippet

    def test_extract_snippet_no_match_zero_context(self):
        snippet = _extract_snippet("hello world", ["关键字"], context_chars=0)
        assert snippet == "hello world"


# ---------------------------------------------------------------------------
# Repository tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSearchTurnSummaries:
    """search_turn_summaries 查询测试。"""

    async def test_empty_keywords(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        results = await search_turn_summaries(
            sessions[1],
            ids["cid"],
            [],
            None,
            None,
            10,
            100,
        )
        assert results == []


@pytest.mark.asyncio
class TestSearchSessionNotes:
    """search_session_notes 查询测试。"""

    async def test_empty_keywords(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        results = await search_session_notes(
            sessions[1],
            ids["cid"],
            [],
            None,
            None,
            10,
            100,
        )
        assert results == []


@pytest.mark.asyncio
class TestSearchCrisisTopics:
    """search_crisis_topics 查询测试。"""

    async def test_empty_keywords(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        results = await search_crisis_topics(
            sessions[1],
            ids["cid"],
            [],
            None,
            None,
            10,
        )
        assert results == []


@pytest.mark.asyncio
class TestSearchDailyReports:
    """search_daily_reports 查询测试。"""

    async def test_empty_keywords(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        results = await search_daily_reports(
            sessions[1],
            ids["cid"],
            [],
            REPORT_DATE - timedelta(days=7),
            REPORT_DATE,
            10,
            100,
        )
        assert results == []


@pytest.mark.asyncio
class TestFetchTurn:
    """fetch_turn 查询测试。"""

    async def test_fetch_existing_turn(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        bundle = await fetch_turn(sessions[1], ids["sid1"], 1, context_turns=0)
        assert bundle is not None
        assert bundle["turn_number"] == 1
        assert bundle["turn_summary"] is not None
        assert bundle["crisis_detected"] is True

    async def test_fetch_non_existent_turn(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        bundle = await fetch_turn(sessions[1], ids["sid1"], 999, context_turns=0)
        assert bundle is not None
        assert bundle["turn_summary"] is None

    async def test_fetch_non_existent_session(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        bad_sid = f"{uuid.uuid4()}"
        bundle = await fetch_turn(sessions[1], bad_sid, 1, context_turns=0)
        assert bundle is None


@pytest.mark.asyncio
class TestFetchNotes:
    """fetch_notes 查询测试。"""

    async def test_fetch_existing_notes(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        bundle = await fetch_notes(sessions[1], ids["sid1"])
        assert bundle is not None
        assert "session_notes" in bundle

    async def test_fetch_non_existent(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        bad_sid = f"{uuid.uuid4()}"
        bundle = await fetch_notes(sessions[1], bad_sid)
        assert bundle is None


@pytest.mark.asyncio
class TestFetchReport:
    """fetch_report 查询测试。"""

    async def test_fetch_existing_report(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)

        row = (
            await sessions[1].execute(
                text("SELECT id FROM daily_reports WHERE child_user_id = :cuid LIMIT 1"),
                {"cuid": ids["cid"]},
            )
        ).one_or_none()
        assert row is not None

        bundle = await fetch_report(sessions[1], str(row[0]))
        assert bundle is not None
        assert "id" in bundle
        assert "today_overview" in bundle
        assert "what_was_discussed" in bundle
        assert "child_user_id" in bundle

    async def test_fetch_non_existent(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        bad_id = f"{uuid.uuid4()}"
        bundle = await fetch_report(sessions[1], bad_id)
        assert bundle is None
