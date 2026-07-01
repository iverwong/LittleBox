"""Expert 域工具 handler 测试：入参校验 + 错误路径。

测试通过 mock Runtime + 模拟 repository 层实现隔离。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from app.core.time import SHANGHAI
from app.domain.expert.context_schema import ExpertContextSchema
from app.domain.expert.schemas import DailyDimensionSummary, SearchSourceType
from app.domain.expert.tools import EXPERT_TOOL_HANDLERS, _fetch_by_ref, _search_history

CUID = uuid.uuid4()
SID = uuid.uuid4()
TURN_SUMMARY_REF = uuid.uuid4()
CRISIS_RECORD_REF = uuid.uuid4()
DAILY_REPORT_REF = uuid.uuid4()
ROLLING_SUMMARY_REF = uuid.uuid4()
REPORT_DATE = date(2026, 6, 23)


def _make_mock_ctx(**overrides: dict) -> ExpertContextSchema:
    """构造最小 ExpertContextSchema（mock 资源字段）。"""
    defaults = dict(
        child_user_id=CUID,
        owned_session_ids=frozenset({SID}),
        session_id=SID,
        report_date=REPORT_DATE,
        dimension_summary=DailyDimensionSummary(peak=0.0, mean=0.0, high_ratio=0.0),
        crisis_detected_today=False,
        max_output_attempts=3,
        token_budget=100_000,
        child_profile=MagicMock(),
        settings=MagicMock(),
        db_session_factory=MagicMock(),
        shared_http_client=MagicMock(),
    )
    defaults.update(overrides)
    return ExpertContextSchema(**defaults)


def _make_mock_runtime(**ctx_overrides: dict) -> SimpleNamespace:
    """构造 mock Runtime[ExpertContextSchema]（SimpleNamespace 替代）。"""
    return SimpleNamespace(context=_make_mock_ctx(**ctx_overrides))


# ---------------------------------------------------------------------------
# _search_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSearchHistoryHandler:
    """_search_history 工具 handler 测试。"""

    async def test_valid_args_returns_toolmessage(self):
        """有效入参应返回 ToolMessage,内容含 has_more + match_list。"""
        runtime = _make_mock_runtime()
        args = {"keywords": ["游戏"], "source": SearchSourceType.TURN_SUMMARY.value}

        with patch(
            "app.domain.expert.tools.search_turn_summaries",
            AsyncMock(
                return_value={"has_more": False, "match_list": []},
            ),
        ) as mock_ts:
            result = await _search_history(args, runtime, "call-1")

        assert result.tool_call_id == "call-1"
        payload = json.loads(result.content)
        assert payload == {"has_more": False, "match_list": []}
        mock_ts.assert_awaited_once()

    async def test_invalid_args_returns_error(self):
        """入参校验失败应返回 error ToolMessage。"""
        runtime = _make_mock_runtime()
        # keywords 空列表 → ValidationError
        args = {"keywords": [], "source": SearchSourceType.TURN_SUMMARY.value}
        result = await _search_history(args, runtime, "call-err")
        payload = json.loads(result.content)
        assert "error" in payload
        assert "validation_errors" in payload

    async def test_start_date_after_end_date_returns_error(self):
        """start_date > end_date 应返回 error ToolMessage。"""
        runtime = _make_mock_runtime()
        args = {
            "keywords": ["游戏"],
            "source": SearchSourceType.TURN_SUMMARY.value,
            "start_date": "2026-06-25",
            "end_date": "2026-06-20",
        }
        result = await _search_history(args, runtime, "call-err")
        payload = json.loads(result.content)
        assert "error" in payload
        assert "start_date 不能晚于 end_date" in payload["error"]

    async def test_end_date_equals_report_date_returns_error(self):
        """end_date == report_date 应返回 error。"""
        runtime = _make_mock_runtime()
        args = {
            "keywords": ["游戏"],
            "source": SearchSourceType.TURN_SUMMARY.value,
            "start_date": str(REPORT_DATE - timedelta(days=1)),
            "end_date": str(REPORT_DATE),
        }
        result = await _search_history(args, runtime, "call-err")
        payload = json.loads(result.content)
        assert "error" in payload
        assert "end_date 需在报告日期之前" in payload["error"]

    async def test_keyword_validation_error(self):
        """单字符关键词应导致 Pydantic 校验失败。"""
        runtime = _make_mock_runtime()
        args = {"keywords": ["a"], "source": SearchSourceType.TURN_SUMMARY.value}
        result = await _search_history(args, runtime, "call-err")
        payload = json.loads(result.content)
        assert "error" in payload

    async def test_source_filter(self):
        """source 单值应只调对应 repository 函数。"""
        runtime = _make_mock_runtime()
        args = {"keywords": ["游戏"], "source": SearchSourceType.TURN_SUMMARY.value}

        with (
            patch(
                "app.domain.expert.tools.search_turn_summaries",
                AsyncMock(return_value={"has_more": False, "match_list": []}),
            ) as mock_ts,
            patch(
                "app.domain.expert.tools.search_session_notes",
                AsyncMock(return_value={"has_more": False, "match_list": []}),
            ) as mock_sn,
            patch(
                "app.domain.expert.tools.search_daily_reports",
                AsyncMock(return_value={"has_more": False, "match_list": []}),
            ) as mock_dr,
        ):
            await _search_history(args, runtime, "call-1")

        mock_ts.assert_awaited_once()
        mock_sn.assert_not_awaited()
        mock_dr.assert_not_awaited()

    async def test_keyword_strips_whitespace_and_dedups(self):
        """keyword validator 应 strip 空白 + 去重,但仍传入完整清理后的列表。"""
        runtime = _make_mock_runtime()
        args = {
            "keywords": ["  游戏  ", "游戏", "学校"],
            "source": SearchSourceType.TURN_SUMMARY.value,
        }

        with patch(
            "app.domain.expert.tools.search_turn_summaries",
            AsyncMock(return_value={"has_more": False, "match_list": []}),
        ) as mock_ts:
            await _search_history(args, runtime, "call-1")

        # schema 校验后清掉空白 + dedupe → ["游戏", "学校"]
        passed_keywords = mock_ts.await_args[0][2]
        assert passed_keywords == ["游戏", "学校"]


@pytest.mark.asyncio
class TestFetchByRefHandler:
    """_fetch_by_ref 工具 handler 测试。"""

    async def test_invalid_args_returns_error(self):
        """search_source 缺失应触发 ValidationError。"""
        runtime = _make_mock_runtime()
        args = {"ref": str(ROLLING_SUMMARY_REF)}  # 缺 search_source
        result = await _fetch_by_ref(args, runtime, "call-err")
        payload = json.loads(result.content)
        assert "error" in payload
        assert "validation_errors" in payload

    async def test_invalid_uuid_returns_error(self):
        """ref 非 UUID 格式应触发 ValidationError。"""
        runtime = _make_mock_runtime()
        args = {
            "search_source": SearchSourceType.SESSION_NOTES.value,
            "ref": "not-a-uuid",
        }
        result = await _fetch_by_ref(args, runtime, "call-err")
        payload = json.loads(result.content)
        assert "error" in payload

    async def test_turn_summary_routes_to_fetch_turn_messages(self):
        """turn_summary → fetch_turn_messages。"""
        runtime = _make_mock_runtime()
        args = {
            "search_source": SearchSourceType.TURN_SUMMARY.value,
            "ref": str(TURN_SUMMARY_REF),
        }
        with patch(
            "app.domain.expert.tools.fetch_turn_messages",
            new=AsyncMock(return_value="[]"),
        ) as m_fetch_turn:
            result = await _fetch_by_ref(args, runtime, "call-1")
        assert result.tool_call_id == "call-1"
        payload = json.loads(result.content)
        assert payload == []
        m_fetch_turn.assert_awaited_once_with(
            ANY,
            CUID,
            SearchSourceType.TURN_SUMMARY,
            TURN_SUMMARY_REF,
            0,
        )

    async def test_crisis_topic_routes_to_fetch_turn_messages(self):
        """crisis_topic → fetch_turn_messages。"""
        runtime = _make_mock_runtime()
        args = {
            "search_source": SearchSourceType.CRISIS_TOPIC.value,
            "ref": str(CRISIS_RECORD_REF),
        }
        with patch(
            "app.domain.expert.tools.fetch_turn_messages",
            AsyncMock(return_value="[]"),
        ) as m_fetch:
            result = await _fetch_by_ref(args, runtime, "call-1")
        assert result.tool_call_id == "call-1"
        m_fetch.assert_awaited_once()
        assert m_fetch.await_args[0][2] == SearchSourceType.CRISIS_TOPIC

    async def test_session_notes_routes_to_fetch_notes(self):
        """session_notes → fetch_notes,ref 即 RollingSummary.id。"""
        runtime = _make_mock_runtime()
        args = {
            "search_source": SearchSourceType.SESSION_NOTES.value,
            "ref": str(ROLLING_SUMMARY_REF),
        }
        notes_payload = json.dumps(
            {
                "session_id": str(SID),
                "session_notes": "情绪稳定",
                "updated_at": "2026-06-22T00:00:00+00:00",
            }
        )
        with patch(
            "app.domain.expert.tools.fetch_notes",
            AsyncMock(return_value=notes_payload),
        ) as m_fetch_notes:
            result = await _fetch_by_ref(args, runtime, "call-1")
        assert result.content == notes_payload
        m_fetch_notes.assert_awaited_once()
        assert m_fetch_notes.await_args[0][1] == CUID
        assert m_fetch_notes.await_args[0][2] == ROLLING_SUMMARY_REF

    async def test_daily_report_routes_to_fetch_report(self):
        """daily_report → fetch_report,ref 即 DailyReport.id。"""
        runtime = _make_mock_runtime()
        args = {
            "search_source": SearchSourceType.DAILY_REPORT.value,
            "ref": str(DAILY_REPORT_REF),
        }
        report_payload = json.dumps(
            {
                "session_id": str(SID),
                "report_date": "2026-06-22",
                "today_overview": "平稳",
                "what_was_discussed": "学校",
                "emotion_changes": "无",
                "noteworthy": "无",
                "suggestions": "保持",
                "anomaly_periods": "无",
                "overall_status": "stable",
                "degraded": False,
            }
        )
        with patch(
            "app.domain.expert.tools.fetch_report",
            AsyncMock(return_value=report_payload),
        ) as m_fetch_report:
            result = await _fetch_by_ref(args, runtime, "call-1")
        assert result.content == report_payload
        m_fetch_report.assert_awaited_once()
        assert m_fetch_report.await_args[0][1] == CUID
        assert m_fetch_report.await_args[0][2] == DAILY_REPORT_REF

    async def test_repo_returns_none_yields_error_toolmessage(self):
        """repository 返回 None 时,handler 应发 error ToolMessage(带 tool_call_id)。"""
        runtime = _make_mock_runtime()
        args = {
            "search_source": SearchSourceType.DAILY_REPORT.value,
            "ref": str(uuid.uuid4()),  # 不存在的 ref
        }
        with patch(
            "app.domain.expert.tools.fetch_report",
            AsyncMock(return_value=None),
        ):
            result = await _fetch_by_ref(args, runtime, "call-err")
        assert result.tool_call_id == "call-err"
        payload = json.loads(result.content)
        assert "error" in payload
        assert "ref 参数有误" in payload["error"]


class TestExportedHandlers:
    """EXPERT_TOOL_HANDLERS 导出字典测试。"""

    def test_contains_both_handlers(self):
        assert "SearchHistoryInput" in EXPERT_TOOL_HANDLERS
        assert "FetchByRefInput" in EXPERT_TOOL_HANDLERS
        assert callable(EXPERT_TOOL_HANDLERS["SearchHistoryInput"])
        assert callable(EXPERT_TOOL_HANDLERS["FetchByRefInput"])
