import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Integer, Float, Boolean, Text, text, func, Index
from sqlalchemy.dialects.postgresql import UUID, TIMESTAMP, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BaseMixin


class AuditRecord(BaseMixin, Base):
    """审查记录。每轮对话一条，保留原始打分。"""
    __tablename__ = "audit_records"
    __table_args__ = (
        Index("idx_audit_session", "session_id", "turn_number"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False,
    )
    turn_number: Mapped[int] = mapped_column(Integer, nullable=False)
    dimension_scores: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True,
        comment="AuditDimensionScores JSON：7 维度 score(0-9) + detail；"
               "供日终专家按维度诊断与跨日聚合；需要综合分时由代码派生 max(score)，不单独存储",
    )
    crisis_detected: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False,
    )
    crisis_topic: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    guidance_injection: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="注入的指引内容",
    )
    redline_triggered: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False,
        comment="家长红线命中 0/1",
    )
    redline_detail: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="命中的红线内容",
    )
    notify_sent: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False,
    )


class RollingSummary(BaseMixin, Base):
    """滚动摘要。每个 session 一条，每轮 upsert 更新。"""
    __tablename__ = "rolling_summaries"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"),
        unique=True, nullable=False,
    )
    last_turn: Mapped[int] = mapped_column(Integer, nullable=False)
    crisis_locked: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False,
        comment="crisis 粘性接管标志。一旦命中 crisis 置 true，该 session 剩余轮次全部由危机 LLM 接管；"
               "session 内不可逆，仅开启新 session 可重置。redline 不粘性，每轮重评估。",
    )
    session_notes: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="风控视角的跨轮叙事笔记（TEXT），审查 Agent 按固定骨架整段重写维护："
               "话题脉络 / 风险观察 / 情绪走向 / 家长关注点回应。"
               "供审查自身跨轮复用 + 日终专家生成家长报告；不注入主 LLM，避免风控判断泄漏",
    )
    turn_summaries: Mapped[Optional[list]] = mapped_column(
        JSONB, nullable=True,
        comment="list[TurnSummaryEntry] JSON：每轮客观中立短摘要（turn + summary）；"
               "供主对话图超窗压缩时注入主 LLM；"
               "日终专家时序分析直接读 audit_records.dimension_scores 原始数据，更精细",
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
