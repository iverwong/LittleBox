"""Expert 域 Pydantic schema 校验测试。"""

from __future__ import annotations

import pytest
from app.core.enums import DailyStatus
from app.domain.expert.schemas import (
    ExpertReportSchema,
    FetchByRefInput,
    SearchHistoryInput,
)
from pydantic import ValidationError


class TestSearchHistoryInput:
    """SearchHistoryInput schema 校验测试。"""

    def test_valid_minimal(self):
        """最简有效入参：keywords + source（必填）。"""
        s = SearchHistoryInput(keywords=["游戏"], source="turn_summary")
        assert s.keywords == ["游戏"]
        assert s.source == "turn_summary"
        assert s.limit == 10
        assert s.context_chars == 80
        assert s.start_date is None
        assert s.end_date is None

    def test_valid_full(self):
        """全部可选字段填充。"""
        from datetime import date

        s = SearchHistoryInput(
            keywords=["游戏", "学校"],
            source="daily_report",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 20),
            limit=5,
            context_chars=50,
        )
        assert len(s.keywords) == 2
        assert s.limit == 5
        assert s.source == "daily_report"

    def test_missing_source_required(self):
        """source 必填,缺失应报错。"""
        with pytest.raises(ValidationError):
            SearchHistoryInput(keywords=["游戏"])

    def test_invalid_source_literal(self):
        """source 传入非 Literal 候选应报错(Pydantic Literal 收口)。"""
        with pytest.raises(ValidationError):
            SearchHistoryInput(keywords=["test"], source="invalid_source")

    def test_keyword_too_short(self):
        """关键词长度不足 2 字符应报错。"""
        with pytest.raises(ValidationError):
            SearchHistoryInput(keywords=["a"], source="turn_summary")

    def test_keyword_exactly_2_chars(self):
        """关键词刚好 2 字符应通过。"""
        s = SearchHistoryInput(keywords=["ab"], source="turn_summary")
        assert "ab" in s.keywords

    def test_empty_keywords_list(self):
        """关键词空列表应报错。"""
        with pytest.raises(ValidationError):
            SearchHistoryInput(keywords=[], source="turn_summary")

    def test_keywords_max_8(self):
        """关键词最多 8 个。"""
        keywords = ["a1", "b2", "c3", "d4", "e5", "f6", "g7", "h8"]
        s = SearchHistoryInput(keywords=keywords, source="turn_summary")
        assert len(s.keywords) == 8

    def test_keywords_exceed_8(self):
        """关键词超过 8 个应报错。"""
        with pytest.raises(ValidationError):
            SearchHistoryInput(keywords=[f"k{i}" for i in range(9)], source="turn_summary")

    def test_limit_bounds(self):
        """limit 超出 1-50 范围应报错。"""
        with pytest.raises(ValidationError):
            SearchHistoryInput(keywords=["test"], source="turn_summary", limit=0)
        with pytest.raises(ValidationError):
            SearchHistoryInput(keywords=["test"], source="turn_summary", limit=51)

    def test_context_chars_bounds(self):
        """context_chars 超出 0-400 范围应报错。"""
        with pytest.raises(ValidationError):
            SearchHistoryInput(keywords=["test"], source="turn_summary", context_chars=-1)
        with pytest.raises(ValidationError):
            SearchHistoryInput(keywords=["test"], source="turn_summary", context_chars=401)

    def test_context_chars_zero(self):
        """context_chars=0 应通过。"""
        s = SearchHistoryInput(keywords=["test"], source="turn_summary", context_chars=0)
        assert s.context_chars == 0


class TestFetchByRefInput:
    """FetchByRefInput schema 校验测试。"""

    def test_valid_turn_ref(self):
        """有效的 turn 格式引用。"""
        f = FetchByRefInput(ref="turn:00000000-0000-0000-0000-000000000001#3")
        assert f.ref.startswith("turn:")
        assert f.context_turns == 0

    def test_valid_notes_ref(self):
        """有效的 notes 格式引用。"""
        f = FetchByRefInput(ref="notes:00000000-0000-0000-0000-000000000001")
        assert f.ref == "notes:00000000-0000-0000-0000-000000000001"

    def test_valid_report_ref(self):
        """有效的 report 格式引用。"""
        f = FetchByRefInput(ref="report:00000000-0000-0000-0000-000000000001")
        assert f.ref == "report:00000000-0000-0000-0000-000000000001"

    def test_context_turns_bounds(self):
        """context_turns 超过 0-3 范围应报错。"""
        valid_ref = "turn:00000000-0000-0000-0000-000000000001#1"
        with pytest.raises(ValidationError):
            FetchByRefInput(ref=valid_ref, context_turns=-1)
        with pytest.raises(ValidationError):
            FetchByRefInput(ref=valid_ref, context_turns=4)

    def test_empty_ref(self):
        """空 ref 应报错。"""
        with pytest.raises(ValidationError):
            FetchByRefInput(ref="")


class TestExpertReportSchema:
    """ExpertReportSchema schema 校验测试。"""

    def test_valid_minimal(self):
        """最简有效报告。"""
        r = ExpertReportSchema(
            overall_status=DailyStatus.stable,
            today_overview="平稳",
            what_was_discussed="学校",
            emotion_changes="无",
            noteworthy="无",
            suggestions="保持",
            anomaly_periods="无",
        )
        assert r.overall_status == DailyStatus.stable
        assert r.degraded is False

    def test_valid_alert_status(self):
        """alert 状态报告。"""
        r = ExpertReportSchema(
            overall_status=DailyStatus.alert,
            today_overview="异常",
            what_was_discussed="冲突",
            emotion_changes="焦虑",
            noteworthy="需关注",
            suggestions="联系",
            anomaly_periods="晚上",
        )
        assert r.overall_status == DailyStatus.alert

    def test_degraded_true(self):
        """degraded=True 应通过。"""
        r = ExpertReportSchema(
            overall_status=DailyStatus.attention,
            degraded=True,
            today_overview="降级",
            what_was_discussed="降级",
            emotion_changes="降级",
            noteworthy="降级",
            suggestions="降级",
            anomaly_periods="降级",
        )
        assert r.degraded is True

    def test_empty_today_overview(self):
        """today_overview 空串应报错。"""
        with pytest.raises(ValidationError):
            ExpertReportSchema(
                overall_status=DailyStatus.stable,
                today_overview="",
                what_was_discussed="学校",
                emotion_changes="无",
                noteworthy="无",
                suggestions="保持",
                anomaly_periods="无",
            )

    def test_empty_required_field(self):
        """任一必填字符串字段空串应报错。"""
        base = dict(
            overall_status=DailyStatus.stable,
            today_overview="a",
            what_was_discussed="a",
            emotion_changes="a",
            noteworthy="a",
            suggestions="a",
            anomaly_periods="a",
        )
        for field in (
            "today_overview",
            "what_was_discussed",
            "emotion_changes",
            "noteworthy",
            "suggestions",
            "anomaly_periods",
        ):
            kwargs = {**base, field: ""}
            with pytest.raises(ValidationError):
                ExpertReportSchema(**kwargs)
