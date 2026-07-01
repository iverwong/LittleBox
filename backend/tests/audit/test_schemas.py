"""M8 audit schemas 单元测试：validator 边界覆盖 ≥27 条。"""
from __future__ import annotations

import pytest
from app.domain.audit.schemas import (
    AuditDimensionScores,
    AuditOutputSchema,
    AuditSignalsPayload,
    ReplaceInNotes,
    TurnSummaryEntry,
)
from pydantic import ValidationError

pytestmark = pytest.mark.audit


class TestAuditDimensionScores:
    """6 维度 × Field(ge=0, le=9) 范围校验。"""

    @pytest.mark.parametrize("field", [
        "emotional", "social", "values",
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
    """``TurnSummaryEntry`` 是历史 JSONB 列的临时 Pydantic 形态;M11 拆出独立
    ``turn_summaries`` 表后,``created_at`` 由 ORM 行基混入派生,不再由 schema 验证。

    此处保留 ``summary`` 长度兜底,M12 若评估可下线此类(Pydantic 形态将无对应数据)。
    """

    def test_summary_max_length_ok(self):
        s = TurnSummaryEntry(turn_number=1, summary="a" * 100)
        assert len(s.summary) == 100

    def test_summary_too_long(self):
        with pytest.raises(ValidationError):
            TurnSummaryEntry(turn_number=1, summary="a" * 101)

    def test_summary_minimal(self):
        s = TurnSummaryEntry(turn_number=1, summary="")
        assert s.summary == ""


class TestAuditOutputSchema:
    """guidance/turn_summary 长度 + crisis/redline 联动。"""

    def test_guidance_max_length_ok(self):
        s = AuditOutputSchema(
            dimension_scores=AuditDimensionScores(),
            guidance_injection="a" * 300,
            turn_summary="ok",
        )
        assert s.guidance_injection and len(s.guidance_injection) == 300

    def test_guidance_too_long(self):
        with pytest.raises(ValidationError):
            AuditOutputSchema(
                dimension_scores=AuditDimensionScores(),
                guidance_injection="a" * 301,
                turn_summary="ok",
            )

    def test_turn_summary_max_length_ok(self):
        s = AuditOutputSchema(
            dimension_scores=AuditDimensionScores(),
            guidance_injection="ok",
            turn_summary="a" * 100,
        )
        assert len(s.turn_summary) == 100

    def test_turn_summary_too_long(self):
        with pytest.raises(ValidationError):
            AuditOutputSchema(
                dimension_scores=AuditDimensionScores(),
                guidance_injection="ok",
                turn_summary="a" * 101,
            )

    # crisis 联动
    def test_crisis_detected_without_topic_raises(self):
        with pytest.raises(ValidationError, match="crisis_detected=True"):
            AuditOutputSchema(
                dimension_scores=AuditDimensionScores(),
                crisis_detected=True,
                crisis_topic=None,
                guidance_injection="ok",
                turn_summary="ok",
            )

    def test_crisis_not_detected_with_topic_raises(self):
        with pytest.raises(ValidationError, match="crisis_detected=False"):
            AuditOutputSchema(
                dimension_scores=AuditDimensionScores(),
                crisis_detected=False,
                crisis_topic="some topic",
                guidance_injection="ok",
                turn_summary="ok",
            )

    def test_crisis_detected_with_topic_ok(self):
        s = AuditOutputSchema(
            dimension_scores=AuditDimensionScores(),
            crisis_detected=True,
            crisis_topic="提到自残倾向",
            guidance_injection="ok",
            turn_summary="ok",
        )
        assert s.crisis_topic == "提到自残倾向"


class TestReplaceInNotes:
    """old_str 非空约束 + new_str 可空(min_length=0)。"""

    def test_old_str_empty_raises(self):
        with pytest.raises(ValidationError):
            ReplaceInNotes(old_str="", new_str="replacement")

    def test_new_str_empty_ok(self):
        s = ReplaceInNotes(old_str="original", new_str="")
        assert s.old_str == "original"
        assert s.new_str == ""

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
                guidance_injection="ok",
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
                    guidance_injection="ok",
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
