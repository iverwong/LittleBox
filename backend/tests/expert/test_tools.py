"""Expert 域工具 handler 测试：入参校验 + 错误路径。

测试通过 mock Runtime + 模拟 repository 层实现隔离。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.core.time import SHANGHAI
from app.domain.expert.context_schema import ExpertContextSchema
from app.domain.expert.tools import EXPERT_TOOL_HANDLERS, _fetch_by_ref, _search_history

CUID = uuid.uuid4()
SID = uuid.uuid4()
REPORT_DATE = date(2026, 6, 23)


def _make_mock_ctx(**overrides: dict) -> ExpertContextSchema:
    """构造最小 ExpertContextSchema（mock 资源字段）。"""
    day_start = datetime.combine(REPORT_DATE, datetime.min.time()).replace(
        tzinfo=SHANGHAI,
    ) + timedelta(hours=4)
    day_end = day_start + timedelta(days=1)
    defaults = dict(
        child_user_id=CUID,
        owned_session_ids=frozenset({SID}),
        session_id=SID,
        report_date=REPORT_DATE,
        day_start=day_start,
        day_end=day_end,
        dimension_summary={},
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
        """有效入参应返回 ToolMessage。"""
        runtime = _make_mock_runtime()
        args = {"keywords": ["游戏"], "source": "turn_summary"}

        with patch(
            "app.domain.expert.tools.search_turn_summaries",
            AsyncMock(return_value=[]),
        ) as mock_ts:
            result = await _search_history(args, runtime, "call-1")

        assert result.tool_call_id == "call-1"
        payload = json.loads(result.content)
        assert "results" in payload
        assert payload["total"] == 0
        mock_ts.assert_awaited_once()

    async def test_invalid_args_returns_error(self):
        """入参校验失败应返回 error ToolMessage。"""
        runtime = _make_mock_runtime()
        # keywords 空列表 → ValidationError
        args = {"keywords": [], "source": "turn_summary"}
        result = await _search_history(args, runtime, "call-err")
        payload = json.loads(result.content)
        assert "error" in payload

    async def test_start_date_after_end_date_returns_error(self):
        """start_date > end_date 应返回 error ToolMessage。"""
        runtime = _make_mock_runtime()
        args = {
            "keywords": ["游戏"],
            "source": "turn_summary",
            "start_date": "2026-06-25",
            "end_date": "2026-06-20",
        }
        result = await _search_history(args, runtime, "call-err")
        payload = json.loads(result.content)
        assert "error" in payload
        assert "start_date cannot be after end_date" in payload["error"]

    async def test_end_date_equals_report_date_returns_error(self):
        """end_date == report_date 应返回 error。"""
        runtime = _make_mock_runtime()
        args = {
            "keywords": ["游戏"],
            "source": "turn_summary",
            "start_date": str(REPORT_DATE - timedelta(days=1)),
            "end_date": str(REPORT_DATE),
        }
        result = await _search_history(args, runtime, "call-err")
        payload = json.loads(result.content)
        assert "error" in payload
        assert "end_date must be before report_date" in payload["error"]

    async def test_keyword_validation_error(self):
        """单字符关键词应导致 Pydantic 校验失败。"""
        runtime = _make_mock_runtime()
        args = {"keywords": ["a"], "source": "turn_summary"}
        result = await _search_history(args, runtime, "call-err")
        payload = json.loads(result.content)
        assert "error" in payload

    async def test_source_filter(self):
        """source 单值应只调对应 repository 函数。"""
        runtime = _make_mock_runtime()
        args = {"keywords": ["游戏"], "source": "turn_summary"}

        with (
            patch(
                "app.domain.expert.tools.search_turn_summaries",
                AsyncMock(return_value=[]),
            ) as mock_ts,
            patch(
                "app.domain.expert.tools.search_session_notes",
                AsyncMock(return_value=[]),
            ) as mock_sn,
            patch(
                "app.domain.expert.tools.search_daily_reports",
                AsyncMock(return_value=[]),
            ) as mock_dr,
        ):
            await _search_history(args, runtime, "call-1")

        mock_ts.assert_awaited_once()
        mock_sn.assert_not_awaited()
        mock_dr.assert_not_awaited()


@pytest.mark.asyncio
class TestFetchByRefHandler:
    """_fetch_by_ref 工具 handler 测试。"""

    async def test_invalid_ref_format_returns_error(self):
        """不合法 ref 格式应返回 error ToolMessage。"""
        runtime = _make_mock_runtime()
        args = {"ref": "invalid:ref:format"}
        result = await _fetch_by_ref(args, runtime, "call-err")
        payload = json.loads(result.content)
        assert "error" in payload
        assert "invalid ref format" in payload["error"]

    async def test_turn_ref_ownership_check(self, monkeypatch):
        """turn 引用中 session 不在 owned 列表应返回 error。"""
        other_sid = uuid.uuid4()
        args = {"ref": f"turn:{other_sid}#1"}
        runtime = _make_mock_runtime(owned_session_ids=frozenset({uuid.uuid4()}))
        result = await _fetch_by_ref(args, runtime, "call-err")
        payload = json.loads(result.content)
        assert "error" in payload
        assert "not owned by child" in payload["error"]

    async def test_notes_ref_ownership_check(self):
        """notes 引用中 session 不在 owned 列表应返回 error。"""
        other_sid = uuid.uuid4()
        args = {"ref": f"notes:{other_sid}"}
        runtime = _make_mock_runtime(owned_session_ids=frozenset({uuid.uuid4()}))
        result = await _fetch_by_ref(args, runtime, "call-err")
        payload = json.loads(result.content)
        assert "error" in payload
        assert "not owned by child" in payload["error"]

    async def test_turn_not_found(self):
        """turn 不存在应返回 error。"""
        sid = uuid.uuid4()
        args = {"ref": f"turn:{sid}#99"}
        runtime = _make_mock_runtime(owned_session_ids=frozenset({sid}))
        with patch("app.domain.expert.tools.fetch_turn", AsyncMock(return_value=None)):
            result = await _fetch_by_ref(args, runtime, "call-err")

        payload = json.loads(result.content)
        assert "error" in payload
        assert "not found" in payload["error"]

    async def test_valid_turn_ref(self):
        """有效 turn 引用应返回 bundle。"""
        sid = uuid.uuid4()
        bundle = {"session_id": str(sid), "turn_number": 3, "turn_summary": "test"}
        args = {"ref": f"turn:{sid}#3"}
        runtime = _make_mock_runtime(owned_session_ids=frozenset({sid}))
        with patch("app.domain.expert.tools.fetch_turn", AsyncMock(return_value=bundle)):
            result = await _fetch_by_ref(args, runtime, "call-ok")

        payload = json.loads(result.content)
        assert payload["session_id"] == str(sid)
        assert payload["turn_number"] == 3

    async def test_valid_notes_ref(self):
        """有效 notes 引用应返回 bundle。"""
        sid = uuid.uuid4()
        bundle = {"session_id": str(sid), "session_notes": "test notes"}
        args = {"ref": f"notes:{sid}"}
        runtime = _make_mock_runtime(owned_session_ids=frozenset({sid}))
        with patch("app.domain.expert.tools.fetch_notes", AsyncMock(return_value=bundle)):
            result = await _fetch_by_ref(args, runtime, "call-ok")

        payload = json.loads(result.content)
        assert payload["session_id"] == str(sid)
        assert "session_notes" in payload

    async def test_valid_report_ref(self):
        """有效 report 引用应返回 bundle。"""
        rid = uuid.uuid4()
        bundle = {
            "id": str(rid),
            "child_user_id": str(CUID),
            "session_id": str(SID),
            "report_date": "2026-06-22",
            "today_overview": "平稳",
            "what_was_discussed": "学校",
        }
        args = {"ref": f"report:{rid}"}
        runtime = _make_mock_runtime()
        with patch("app.domain.expert.tools.fetch_report", AsyncMock(return_value=bundle)):
            result = await _fetch_by_ref(args, runtime, "call-ok")

        payload = json.loads(result.content)
        assert payload["id"] == str(rid)
        assert payload["today_overview"] == "平稳"

    async def test_report_not_found_returns_error(self):
        """report 不存在时 fetch_report 返回 None,handler 转 error ToolMessage。"""
        rid = uuid.uuid4()
        args = {"ref": f"report:{rid}"}
        runtime = _make_mock_runtime()
        with patch(
            "app.domain.expert.tools.fetch_report",
            AsyncMock(return_value=None),
        ):
            result = await _fetch_by_ref(args, runtime, "call-err")

        payload = json.loads(result.content)
        assert "error" in payload
        assert "not found" in payload["error"]

    async def test_fetch_by_ref_report_owner_mismatch(self):
        """report 不属于 ctx.child_user_id 时应返 not owned by child。

        准备 child A 的 report row,ctx.child_user_id 模拟 child B,验证 owner check 兜住。
        """
        rid = uuid.uuid4()
        child_a_id = uuid.uuid4()
        bundle = {
            "id": str(rid),
            "child_user_id": str(child_a_id),
            "session_id": str(uuid.uuid4()),
            "report_date": "2026-06-22",
            "today_overview": "平稳",
        }
        args = {"ref": f"report:{rid}"}
        # ctx 是 child B(CUID),report 属于 child A
        runtime = _make_mock_runtime()
        with patch("app.domain.expert.tools.fetch_report", AsyncMock(return_value=bundle)):
            result = await _fetch_by_ref(args, runtime, "call-err")

        payload = json.loads(result.content)
        assert "error" in payload
        assert "not owned by child" in payload["error"]


class TestExportedHandlers:
    """EXPERT_TOOL_HANDLERS 导出字典测试。"""

    def test_contains_both_handlers(self):
        assert "SearchHistoryInput" in EXPERT_TOOL_HANDLERS
        assert "FetchByRefInput" in EXPERT_TOOL_HANDLERS
        assert callable(EXPERT_TOOL_HANDLERS["SearchHistoryInput"])
        assert callable(EXPERT_TOOL_HANDLERS["FetchByRefInput"])
