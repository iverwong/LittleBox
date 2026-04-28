import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseMixin
from app.models.enums import DailyStatus, NotificationType


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


class Notification(BaseMixin, Base):
    """家长通知（危机实时推送 / 日常摘要）。"""

    __tablename__ = "notifications"

    parent_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    child_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        comment="关联的 child（可选）；child 删除时 CASCADE 清空，系统通知为 NULL",
    )
    type: Mapped[NotificationType] = mapped_column(nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="MVP 不约束 schema，消费方按 type 解构",
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    read_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )


class DataDeletionRequest(BaseMixin, Base):
    """数据删除请求审计（合规）。仅记录已完成的硬删。"""

    __tablename__ = "data_deletion_requests"

    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),  # 保留 FK：parent 不会被删
        nullable=False,
        comment="发起删除的家长",
    )
    child_id_snapshot: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),  # 无 FK：child 已 CASCADE 删除，仅留 UUID 快照
        nullable=False,
        comment="被删 child 的 user.id（快照）",
    )
    deleted_tables: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="{table: count} 各表删除行数",
    )
    reason: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="触发原因，MVP 固定 'parent_request'",
    )
