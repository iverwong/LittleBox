"""Expert 域仓储层只读查询 SQL 正确性测试。

使用 concurrent_db_sessions fixture 实现真 commit + TRUNCATE 清空。
每测试独立 UUID 簇防冲突。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from app.core.enums import DailyStatus
from app.core.time import SHANGHAI
from app.domain.expert.repository import (
    _extract_snippet,
    fetch_notes,
    fetch_report,
    fetch_turn_messages,
    search_crisis_topics,
    search_daily_reports,
    search_session_notes,
    search_turn_summaries,
)
from app.domain.expert.schemas import SearchSourceType
from sqlalchemy import text
from tests._tables import _GUARD_TABLES as _TABLES

# 注意：pytest.mark.asyncio 只在需要 async 的类上单独标注

REPORT_DATE = date(2026, 6, 23)
# 种子时间锚定到 REPORT_DATE(测试不依赖运行时时间):
# 各会话 created_at 落在 [REPORT_DATE-2, REPORT_DATE-1),与 dt_window(2, 0) 对齐。
NOW = datetime.combine(REPORT_DATE - timedelta(days=1), time(12, 0, 0), tzinfo=SHANGHAI)


def _make_ids() -> dict:
    """每测试生成独立 UUID 簇。"""
    return {
        "fam_id": f"{uuid.uuid4()}",
        "cid": f"{uuid.uuid4()}",
        "other_cid": f"{uuid.uuid4()}",  # 跨 child 取值测试用
        "sid1": f"{uuid.uuid4()}",
        "sid2": f"{uuid.uuid4()}",
    }


async def _seed_data(db, ids: dict):
    """种子数据：family + user + 2 sessions + turn_summaries + notes + crisis + reports。

    turn_summaries 写入新拆出的 ``turn_summaries`` 表(取代旧的
    ``rolling_summaries.turn_summaries`` JSONB 列);rolling_summaries 只放
    last_turn + session_notes。
    """
    fam_id, cid, sid1, sid2 = ids["fam_id"], ids["cid"], ids["sid1"], ids["sid2"]
    other_cid = ids["other_cid"]

    await db.execute(
        text("INSERT INTO families (id) VALUES (:fam_id)"),
        {"fam_id": fam_id},
    )
    await db.execute(
        text("""
            INSERT INTO users (id, family_id, role, is_active)
            VALUES (:cuid, :fam_id, 'child', true),
                   (:other_cid, :fam_id, 'child', true)
        """),
        {"cuid": cid, "other_cid": other_cid, "fam_id": fam_id},
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
    # 旧 ``rolling_summaries.turn_summaries`` JSONB 已下线,改为向 turn_summaries 表写
    for sid, turn, summary in [
        (sid1, 1, "今天在学校玩得很开心"),
        (sid1, 2, "讨论了下周末去哪里玩"),
        (sid2, 1, "今天有点不开心"),
    ]:
        await db.execute(
            text("""
                INSERT INTO turn_summaries (session_id, turn_number, summary, created_at)
                VALUES (:sid, :turn, :summary, :now)
            """),
            {"sid": sid, "turn": turn, "summary": summary, "now": NOW - timedelta(days=1)},
        )
    await db.execute(
        text("""
            INSERT INTO rolling_summaries
                (session_id, last_turn, session_notes, updated_at)
            VALUES (:sid1, 2, :notes1, :now),
                   (:sid2, 1, :notes2, :now)
        """),
        {
            "sid1": sid1,
            "sid2": sid2,
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
                 created_at, updated_at)
            VALUES
                (:cuid, :sid1, :rd1, :status1,
                 :ov1, :wd1, :ec1, :nt1, :sg1, :ap1, :now1, :now1),
                (:cuid, :sid2, :rd2, :status2,
                 :ov2, :wd2, :ec2, :nt2, :sg2, :ap2, :now2, :now2)
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


async def _seed_turn_summaries_for_session(db, sid: str, summaries: list[tuple[int, str]]):
    """辅助:向指定 session 写 turn_summaries(测试 search_turn_summaries 用)。"""

    for turn, summary in summaries:
        await db.execute(
            text("""
                INSERT INTO turn_summaries (session_id, turn_number, summary, created_at)
                VALUES (:sid, :turn, :summary, :now)
            """),
            {"sid": sid, "turn": turn, "summary": summary, "now": NOW - timedelta(hours=1)},
        )
    await db.commit()


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


class TestHelpers:
    """仓储层辅助函数测试。"""

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
# 日期窗口 helper:供 search_* 测试统一构造 dt 范围
# ---------------------------------------------------------------------------


def _dt_window(start_days_ago: int, end_days_ago: int) -> tuple[datetime, datetime]:
    """构造带 SHANGHAI 时区的窗口 [start_dt, end_dt)。"""

    start = datetime.combine(REPORT_DATE - timedelta(days=start_days_ago), time.min, SHANGHAI)
    end = datetime.combine(REPORT_DATE - timedelta(days=end_days_ago - 1), time.min, SHANGHAI)
    return start, end


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
        start, end = _dt_window(2, 0)
        results = await search_turn_summaries(
            sessions[1],
            uuid.UUID(ids["cid"]),
            [],
            start,
            end,
            10,
        )
        assert results == {"has_more": False, "match_list": []}

    async def test_keyword_match_returns_rows(self, concurrent_db_sessions):
        """关键词命中:返回 turn_summaries 行,ref=行 id。"""
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        start, end = _dt_window(2, 0)
        # 使用唯一关键词排除 sessions 注入了 OTHER 数据干扰
        results = await search_turn_summaries(
            sessions[1],
            uuid.UUID(ids["cid"]),
            ["学校"],
            start,
            end,
            10,
        )
        assert results["has_more"] is False
        assert len(results["match_list"]) == 1
        item = results["match_list"][0]
        assert item["source"] == SearchSourceType.TURN_SUMMARY
        assert "学校" in item["snippet"]
        assert "session:" in item["locating"]

    async def test_other_child_is_filtered(self, concurrent_db_sessions):
        """跨 child 取值应被滤掉。"""
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        start, end = _dt_window(2, 0)
        results = await search_turn_summaries(
            sessions[1],
            uuid.UUID(ids["other_cid"]),  # 不属于种子的 child
            ["学校"],
            start,
            end,
            10,
        )
        assert results["match_list"] == []


@pytest.mark.asyncio
class TestSearchSessionNotes:
    """search_session_notes 查询测试。"""

    async def test_empty_keywords(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        start, end = _dt_window(2, 0)
        results = await search_session_notes(
            sessions[1],
            uuid.UUID(ids["cid"]),
            [],
            start,
            end,
            10,
            100,
        )
        assert results == {"has_more": False, "match_list": []}

    async def test_keyword_match(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        start, end = _dt_window(2, 0)
        results = await search_session_notes(
            sessions[1],
            uuid.UUID(ids["cid"]),
            ["情绪"],
            start,
            end,
            10,
            100,
        )
        assert len(results["match_list"]) >= 1
        item = results["match_list"][0]
        assert item["source"] == SearchSourceType.SESSION_NOTES

    async def test_other_child_is_filtered(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        start, end = _dt_window(2, 0)
        results = await search_session_notes(
            sessions[1],
            uuid.UUID(ids["other_cid"]),
            ["情绪"],
            start,
            end,
            10,
            100,
        )
        assert results["match_list"] == []


@pytest.mark.asyncio
class TestSearchCrisisTopics:
    """search_crisis_topics 查询测试。"""

    async def test_empty_keywords(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        start, end = _dt_window(2, 0)
        results = await search_crisis_topics(
            sessions[1],
            uuid.UUID(ids["cid"]),
            [],
            start,
            end,
            10,
        )
        assert results == {"has_more": False, "match_list": []}

    async def test_keyword_match(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        start, end = _dt_window(2, 0)
        results = await search_crisis_topics(
            sessions[1],
            uuid.UUID(ids["cid"]),
            ["校园"],
            start,
            end,
            10,
        )
        assert len(results["match_list"]) == 1
        item = results["match_list"][0]
        assert item["source"] == SearchSourceType.CRISIS_TOPIC
        assert "校园" in item["snippet"]

    async def test_other_child_is_filtered(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        start, end = _dt_window(2, 0)
        results = await search_crisis_topics(
            sessions[1],
            uuid.UUID(ids["other_cid"]),
            ["校园"],
            start,
            end,
            10,
        )
        assert results["match_list"] == []


@pytest.mark.asyncio
class TestSearchDailyReports:
    """search_daily_reports 查询测试。"""

    async def test_empty_keywords(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        start, end = _dt_window(7, 0)
        results = await search_daily_reports(
            sessions[1],
            uuid.UUID(ids["cid"]),
            [],
            start,
            end,
            10,
            100,
        )
        assert results == {"has_more": False, "match_list": []}

    async def test_keyword_match(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        start, end = _dt_window(7, 0)
        results = await search_daily_reports(
            sessions[1],
            uuid.UUID(ids["cid"]),
            ["焦虑"],
            start,
            end,
            10,
            100,
        )
        assert len(results["match_list"]) >= 1
        item = results["match_list"][0]
        assert item["source"] == SearchSourceType.DAILY_REPORT

    async def test_other_child_is_filtered(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        start, end = _dt_window(7, 0)
        results = await search_daily_reports(
            sessions[1],
            uuid.UUID(ids["other_cid"]),
            ["焦虑"],
            start,
            end,
            10,
            100,
        )
        assert results["match_list"] == []


@pytest.mark.asyncio
class TestFetchTurnMessages:
    """fetch_turn_messages 查询测试。"""

    async def _fetch_first_turn_summary_id(self, db, sid: str) -> str:
        row = (
            await db.execute(
                text(
                    "SELECT id FROM turn_summaries WHERE session_id = :sid ORDER BY turn_number LIMIT 1"
                ),
                {"sid": sid},
            )
        ).first()
        assert row is not None
        return str(row[0])

    async def _fetch_first_crisis_record_id(self, db, sid: str) -> str:
        row = (
            await db.execute(
                text(
                    "SELECT id FROM audit_records WHERE session_id = :sid AND crisis_detected = true "
                    "ORDER BY created_at LIMIT 1"
                ),
                {"sid": sid},
            )
        ).first()
        assert row is not None
        return str(row[0])

    async def test_fetch_existing_turn_summary(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        ref = await self._fetch_first_turn_summary_id(sessions[1], ids["sid1"])
        result = await fetch_turn_messages(
            sessions[1],
            uuid.UUID(ids["cid"]),
            SearchSourceType.TURN_SUMMARY,
            uuid.UUID(ref),
            context_turns=0,
        )
        assert result is not None
        messages = json.loads(result)
        assert isinstance(messages, list)
        assert len(messages) >= 1

    async def test_fetch_existing_crisis_record(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        ref = await self._fetch_first_crisis_record_id(sessions[1], ids["sid1"])
        result = await fetch_turn_messages(
            sessions[1],
            uuid.UUID(ids["cid"]),
            SearchSourceType.CRISIS_TOPIC,
            uuid.UUID(ref),
            context_turns=0,
        )
        assert result is not None
        messages = json.loads(result)
        assert isinstance(messages, list)
        assert len(messages) >= 1

    async def test_fetch_non_existent_ref(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        result = await fetch_turn_messages(
            sessions[1],
            uuid.UUID(ids["cid"]),
            SearchSourceType.TURN_SUMMARY,
            uuid.uuid4(),
            context_turns=0,
        )
        assert result is None


@pytest.mark.asyncio
class TestFetchNotes:
    """fetch_notes 查询测试。"""

    async def _fetch_first_rolling_summary_id(self, db, sid: str) -> str:
        row = (
            await db.execute(
                text("SELECT id FROM rolling_summaries WHERE session_id = :sid"),
                {"sid": sid},
            )
        ).first()
        assert row is not None
        return str(row[0])

    async def test_fetch_existing_notes(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        rsid = await self._fetch_first_rolling_summary_id(sessions[1], ids["sid1"])
        bundle = await fetch_notes(
            sessions[1],
            uuid.UUID(ids["cid"]),
            uuid.UUID(rsid),
        )
        assert bundle is not None
        payload = json.loads(bundle)
        assert payload["session_notes"] == "孩子今天聊了很多关于学校的事情"

    async def test_fetch_other_child_returns_none(self, concurrent_db_sessions):
        """跨 child 取值被 join Session 滤掉,返回 None。"""
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        rsid = await self._fetch_first_rolling_summary_id(sessions[1], ids["sid1"])
        bundle = await fetch_notes(
            sessions[1],
            uuid.UUID(ids["other_cid"]),  # 非种子 child
            uuid.UUID(rsid),
        )
        assert bundle is None

    async def test_fetch_non_existent(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        bundle = await fetch_notes(sessions[1], uuid.UUID(ids["cid"]), uuid.uuid4())
        assert bundle is None


@pytest.mark.asyncio
class TestFetchReport:
    """fetch_report 查询测试。"""

    async def test_fetch_existing_report(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)

        # 显式 ORDER BY 让结果集确定(sid1 先行)
        row = (
            await sessions[1].execute(
                text(
                    "SELECT id FROM daily_reports WHERE child_user_id = :cuid "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"cuid": ids["cid"]},
            )
        ).first()
        assert row is not None
        rid = str(row[0])

        bundle = await fetch_report(
            sessions[1],
            uuid.UUID(ids["cid"]),
            uuid.UUID(rid),
        )
        assert bundle is not None
        payload = json.loads(bundle)
        assert payload["today_overview"] == "平稳的一天"
        assert payload["what_was_discussed"] == "玩了游戏"
        assert "session_id" in payload

    async def test_fetch_other_child_returns_none(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)

        row = (
            await sessions[1].execute(
                text(
                    "SELECT id FROM daily_reports WHERE child_user_id = :cuid "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"cuid": ids["cid"]},
            )
        ).first()
        assert row is not None
        rid = str(row[0])

        bundle = await fetch_report(
            sessions[1],
            uuid.UUID(ids["other_cid"]),
            uuid.UUID(rid),
        )
        assert bundle is None

    async def test_fetch_non_existent(self, concurrent_db_sessions):
        sessions = await concurrent_db_sessions(count=2, tables=_TABLES)
        ids = _make_ids()
        await _seed_data(sessions[0], ids)
        bundle = await fetch_report(sessions[1], uuid.UUID(ids["cid"]), uuid.uuid4())
        assert bundle is None
