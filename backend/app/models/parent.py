import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import ForeignKey, Date, Text, text, Index
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseMixin
from app.models.enums import NotificationType, DeletionStatus, DailyStatus


class DailyReport(BaseMixin, Base):
    """日终报告。每孩子每天最多一条。"""
    __tablename__ = "daily_reports"
    __table_args__ = (
        Index("idx_reports_child", "child_user_id", "report_date"),
    )

    child_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
    )
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    overall_status: Mapped[DailyStatus] = mapped_column(
        nullable=False,
        comment="LLM 综合判断的当日整体状态（stable/attention/alert），UI 列表页色彩标识依据",
    )
    dimension_summary: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True,
        comment="DailyDimensionSummary JSON：7 维度当日 peak / mean / high_turns；"
               "代码层从当日 audit_records.dimension_scores 聚合，供 LLM 量化锚点 + UI 雷达图 + 跨日对比",
    )
    content: Mapped[str] = mapped_column(
        Text, nullable=False, comment="markdown 格式报告",
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )


class Notification(BaseMixin, Base):
    """家长通知（危机实时推送 / 日常摘要）。"""
    __tablename__ = "notifications"

    parent_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
    )
    type: Mapped[NotificationType] = mapped_column(nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
    read_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )


class DataDeletionRequest(BaseMixin, Base):
    """数据删除请求追踪，合规要求。"""
    __tablename__ = "data_deletion_requests"

    child_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False,
        comment="发起删除的家长",
    )
    status: Mapped[DeletionStatus] = mapped_column(
        default=DeletionStatus.pending,
        server_default="pending",
        nullable=False,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True,
    )
