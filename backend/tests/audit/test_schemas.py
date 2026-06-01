"""M8 audit schemas 单元测试：validator 边界覆盖 ≥27 条。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.audit import (
    AppendNote,
    AuditDimensionScores,
    AuditOutputSchema,
    AuditSignalsPayload,
    ReplaceInNotes,
    TurnSummaryEntry,
)

pytestmark = pytest.mark.audit


class TestAuditDimensionScores:
    """7 维度 × Field(ge=0, le=9) 范围校验。"""

    @pytest.mark.parametrize("field", [
        "emotional", "social", "romance", "values",
        "boundaries", "academic", "lifestyle",
    ])
    @pytest.mark.parametrize("value,should_pass", [
        (-1, False),
        (0, True),
        (9, True),
        (10, False),
    ])
    def test_range(self, field: str, value: int, should_pass: bool):
        kwargs = {field: value}
        if should_pass:
            instance = AuditDimensionScores(**kwargs)
            assert getattr(instance, field) == value
        else:
            with pytest.raises(ValidationError):
                AuditDimensionScores(**kwargs)


class TestTurnSummaryEntry:
    """summary 长度 / created_at ISO-8601 格式。"""

    def test_summary_max_length_ok(self):
        s = TurnSummaryEntry(
            turn_number=1,
            summary="a" * 100,
            created_at="2026-05-17T10:00:00+00:00",
        )
        assert len(s.summary) == 100

    def test_summary_too_long(self):
        with pytest.raises(ValidationError):
            TurnSummaryEntry(
                turn_number=1,
                summary="a" * 101,
                created_at="2026-05-17T10:00:00+00:00",
            )

    def test_summary_minimal(self):
        s = TurnSummaryEntry(
            turn_number=1,
            summary="",
            created_at="2026-05-17T10:00:00+00:00",
        )
        assert s.summary == ""

    def test_created_at_valid_iso8601_zulu(self):
        s = TurnSummaryEntry(
            turn_number=1,
            summary="ok",
            created_at="2026-05-17T10:00:00Z",
        )
        assert "T" in s.created_at

    def test_created_at_valid_iso8601_offset(self):
        s = TurnSummaryEntry(
            turn_number=1,
            summary="ok",
            created_at="2026-05-17T10:00:00+08:00",
        )
        assert "+" in s.created_at

    def test_created_at_invalid_format(self):
        with pytest.raises(ValidationError):
            TurnSummaryEntry(
                turn_number=1,
                summary="ok",
                created_at="not-a-date",
            )

    def test_created_at_empty_string(self):
        with pytest.raises(ValidationError):
            TurnSummaryEntry(
                turn_number=1,
                summary="ok",
                created_at="",
            )


class TestAuditOutputSchema:
    """guidance/turn_summary 长度 + crisis/redline 联动。"""

    def test_guidance_max_length_ok(self):
        s = AuditOutputSchema(
            dimension_scores=AuditDimensionScores(),
            guidance="a" * 300,
            turn_summary="ok",
        )
        assert len(s.guidance) == 300

    def test_guidance_too_long(self):
        with pytest.raises(ValidationError):
            AuditOutputSchema(
                dimension_scores=AuditDimensionScores(),
                guidance="a" * 301,
                turn_summary="ok",
            )

    def test_turn_summary_max_length_ok(self):
        s = AuditOutputSchema(
            dimension_scores=AuditDimensionScores(),
            guidance="ok",
            turn_summary="a" * 100,
        )
        assert len(s.turn_summary) == 100

    def test_turn_summary_too_long(self):
        with pytest.raises(ValidationError):
            AuditOutputSchema(
                dimension_scores=AuditDimensionScores(),
                guidance="ok",
                turn_summary="a" * 101,
            )

    # crisis 联动
    def test_crisis_detected_without_topic_raises(self):
        with pytest.raises(ValidationError, match="crisis_detected=True"):
            AuditOutputSchema(
                dimension_scores=AuditDimensionScores(),
                crisis_detected=True,
                crisis_topic=None,
                guidance="ok",
                turn_summary="ok",
            )

    def test_crisis_not_detected_with_topic_raises(self):
        with pytest.raises(ValidationError, match="crisis_detected=False"):
            AuditOutputSchema(
                dimension_scores=AuditDimensionScores(),
                crisis_detected=False,
                crisis_topic="some topic",
                guidance="ok",
                turn_summary="ok",
            )

    def test_crisis_detected_with_topic_ok(self):
        s = AuditOutputSchema(
            dimension_scores=AuditDimensionScores(),
            crisis_detected=True,
            crisis_topic="提到自残倾向",
            guidance="ok",
            turn_summary="ok",
        )
        assert s.crisis_topic == "提到自残倾向"

    # redline 联动
    def test_redline_triggered_without_detail_raises(self):
        with pytest.raises(ValidationError, match="redline_triggered=True"):
            AuditOutputSchema(
                dimension_scores=AuditDimensionScores(),
                redline_triggered=True,
                redline_detail=None,
                guidance="ok",
                turn_summary="ok",
            )

    def test_redline_not_triggered_with_detail_raises(self):
        with pytest.raises(ValidationError, match="redline_triggered=False"):
            AuditOutputSchema(
                dimension_scores=AuditDimensionScores(),
                redline_triggered=False,
                redline_detail="some detail",
                guidance="ok",
                turn_summary="ok",
            )

    def test_redline_triggered_with_detail_ok(self):
        s = AuditOutputSchema(
            dimension_scores=AuditDimensionScores(),
            redline_triggered=True,
            redline_detail="涉及暴力言论",
            guidance="ok",
            turn_summary="ok",
        )
        assert s.redline_detail == "涉及暴力言论"


class TestAppendNote:
    """text 非空 + max_length=500。"""

    def test_text_empty_raises(self):
        with pytest.raises(ValidationError):
            AppendNote(text="")

    def test_text_max_length_ok(self):
        s = AppendNote(text="a" * 500)
        assert len(s.text) == 500

    def test_text_too_long(self):
        with pytest.raises(ValidationError):
            AppendNote(text="a" * 501)

    def test_text_normal(self):
        s = AppendNote(text="用户今天情绪不稳定")
        assert s.text == "用户今天情绪不稳定"


class TestReplaceInNotes:
    """old_str / new_str 非空约束。"""

    def test_old_str_empty_raises(self):
        with pytest.raises(ValidationError):
            ReplaceInNotes(old_str="", new_str="replacement")

    def test_new_str_empty_raises(self):
        with pytest.raises(ValidationError):
            ReplaceInNotes(old_str="original", new_str="")

    def test_both_valid(self):
        s = ReplaceInNotes(old_str="旧文本", new_str="新文本")
        assert s.old_str == "旧文本"
        assert s.new_str == "新文本"


class TestAuditSignalsPayload:
    """status 枚举 + signals↔status + error↔status 联动。"""

    # 正常构造
    def test_valid_pending(self):
        s = AuditSignalsPayload(status="pending", turn=1)
        assert s.status == "pending"
        assert s.signals is None
        assert s.error is None

    def test_valid_ready(self):
        s = AuditSignalsPayload(
            status="ready",
            turn=1,
            signals=AuditOutputSchema(
                dimension_scores=AuditDimensionScores(),
                guidance="ok",
                turn_summary="ok",
            ),
        )
        assert s.signals is not None

    def test_valid_failed(self):
        s = AuditSignalsPayload(status="failed", turn=1, error="LLM API timeout")
        assert s.error == "LLM API timeout"

    # 枚举
    def test_invalid_status_raises(self):
        with pytest.raises(ValidationError):
            AuditSignalsPayload(status="unknown", turn=1)

    # signals↔status 联动
    def test_ready_without_signals_raises(self):
        with pytest.raises(ValidationError, match="signals 必须非空"):
            AuditSignalsPayload(status="ready", turn=1)

    def test_pending_with_signals_raises(self):
        with pytest.raises(ValidationError, match="signals 必须为 None"):
            AuditSignalsPayload(
                status="pending",
                turn=1,
                signals=AuditOutputSchema(
                    dimension_scores=AuditDimensionScores(),
                    guidance="ok",
                    turn_summary="ok",
                ),
            )

    # error↔status 联动
    def test_failed_without_error_raises(self):
        with pytest.raises(ValidationError, match="error 必须非空"):
            AuditSignalsPayload(status="failed", turn=1)

    def test_failed_with_empty_error_raises(self):
        with pytest.raises(ValidationError, match="error 必须非空"):
            AuditSignalsPayload(status="failed", turn=1, error="")

    def test_pending_with_error_raises(self):
        with pytest.raises(ValidationError, match="error 必须为 None"):
            AuditSignalsPayload(status="pending", turn=1, error="something")
