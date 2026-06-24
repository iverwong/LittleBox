"""expert 域 ORM 模型。

当前包含 1 张表:`DailyReport`,用于存储日终专家生成的报告。
"""

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, BaseMixin
from app.core.enums import DailyStatus


class DailyReport(BaseMixin, Base):
    """日终报告。每孩子每天最多一条。

    Attributes:
        id: 主键 UUID(继承自 BaseMixin)。
        created_at: 记录创建时间(继承自 BaseMixin)。
        child_user_id: 报告所属孩子 user.id,删除孩子时级联清理。
        report_date: 报告对应的自然日(逻辑日),与时区工具产出的 logical_day 对齐。
        overall_status: 当日整体状态枚举(stable / attention / alert),UI 列表页色彩标识依据。
        dimension_summary: 6 维度当日 peak / mean / high_ratio 的 JSON 聚合,代码层从
            audit_records.dimension_scores 聚合,供 UI 雷达图与跨日对比使用。
        content: markdown 格式报告正文。
        delivered_at: 报告送达时间(可空,未送达则为 NULL)。
    """

    __tablename__ = "daily_reports"
    __table_args__ = (Index("idx_reports_child", "child_user_id", "report_date", unique=True),)

    child_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    report_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        comment="对齐 4 点逻辑日(boundary_hour=4)",
    )
    overall_status: Mapped[DailyStatus] = mapped_column(
        nullable=False,
        comment="LLM 判断 + 危机态代码地板的当日整体状态"
        "(stable/attention/alert),UI 列表页色彩标识依据",
    )
    dimension_summary: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="DailyDimensionSummary JSON:6 维度当日 peak / mean / high_ratio;"
        "代码层从 audit_records.dimension_scores 聚合,"
        "供 UI 雷达图 + 跨日对比使用",
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="markdown 格式报告",
    )
    degraded: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="True 表示降级产物(交卷耗尽 / token 超限),前端展示降级提示",
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
