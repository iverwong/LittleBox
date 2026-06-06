"""Pydantic schemas for API request/response models (transitional aggregator).

本模块在 1.1c 完成后保留至 Phase 6.5 整体删;聚合源已迁到 `app.domain.audit.schemas`。
phase 6.5 之前,旧 `app.schemas.audit` 仍被 `app.state.audit_signals` 引用(1.2 才迁),
本 `__init__.py` 暂无外部调用方,纯过渡占位。
"""
from app.domain.audit.schemas import (
    AppendNote,
    AuditDimensionScores,
    AuditOutputSchema,
    AuditSignalsPayload,
    ReplaceInNotes,
    TurnSummaryEntry,
)

__all__ = [
    "AppendNote",
    "AuditDimensionScores",
    "AuditOutputSchema",
    "AuditSignalsPayload",
    "ReplaceInNotes",
    "TurnSummaryEntry",
]
