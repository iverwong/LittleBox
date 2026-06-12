"""expert 域 ORM(1 张表:DailyReport)。

M12+ 日终专家域填充内容;Phase 6 仅做物理迁出,不改 schema。
"""

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, BaseMixin
from app.core.enums import DailyStatus


class DailyReport(BaseMixin, Base):
    """日终报告。每孩子每天最多一条。"""

    __tablename__ = "daily_reports"
    __table_args__ = (Index("idx_reports_child", "child_user_id", "report_date"),)

    child_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    overall_status: Mapped[DailyStatus] = mapped_column(
        nullable=False,
        comment="LLM 综合判断的当日整体状态（stable/attention/alert），UI 列表页色彩标识依据",
    )
    dimension_summary: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="DailyDimensionSummary JSON：7 维度当日 peak / mean / high_turns；"
        "代码层从 audit_records.dimension_scores 聚合，"
        "供 LLM 量化锚点 + UI 雷达图 + 跨日对比",
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="markdown 格式报告",
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
