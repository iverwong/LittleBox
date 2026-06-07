"""Transitional shim — 6.C 临时保留,6.4 整体删除。

2 张表(AuditRecord / RollingSummary)已迁至 app.domain.audit.models。
"""

from app.domain.audit.models import AuditRecord, RollingSummary

__all__ = ["AuditRecord", "RollingSummary"]
