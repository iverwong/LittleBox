"""audit 域 ORM(2 张表:AuditRecord / RollingSummary)。

D-1 边界:同其他域,不 import 跨域 model。
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, BaseMixin
from app.core.orm_types import PydanticJSONB
from app.domain.audit.schemas import AuditDimensionScores


class AuditRecord(BaseMixin, Base):
    """审查记录。每轮对话一条,保留原始打分。

    Attributes:
        session_id: 被审查对话 session 的外键。
        turn_number: 对话轮次编号,与 ai_turn_counter 对齐。
        target_message_id: 本轮审查锚点(被审查的 ai_msg id),由 enqueue_audit
            从主对话生成器下传。
        dimension_scores: 6 维度评分 JSON(AuditDimensionScores);
            综合分由代码派生 max(score),不单独存储。
        crisis_detected: 是否检测到危机信号。
        crisis_topic: 危机主题描述,crisis_detected=True 时必填。
        guidance_injection: 注入到下一轮主对话的引导文本。
        notify_sent: 危机通知是否已发送。
    """

    __tablename__ = "audit_records"
    __table_args__ = (
        UniqueConstraint("session_id", "turn_number", name="uq_audit_records_session_turn"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    turn_number: Mapped[int] = mapped_column(Integer, nullable=False)
    target_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="被审查的 ai_msg id(本轮审查锚点),由 enqueue_audit 从 me.py generator 传入",
    )
    dimension_scores: Mapped[Optional[AuditDimensionScores]] = mapped_column(
        PydanticJSONB(AuditDimensionScores),
        nullable=True,
        comment="AuditDimensionScores JSON:6 维度 score(0-9) + detail;"
        "供日终专家按维度诊断与跨日聚合;需要综合分时由代码派生 max(score),不单独存储",
    )
    crisis_detected: Mapped[bool] = mapped_column(
        Boolean,
        server_default=text("false"),
        nullable=False,
    )
    crisis_topic: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    guidance_injection: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="注入的指引内容",
    )
    notify_sent: Mapped[bool] = mapped_column(
        Boolean,
        server_default=text("false"),
        nullable=False,
    )


class RollingSummary(BaseMixin, Base):
    """滚动摘要。每个 session 一条,每轮 upsert 更新。

    Attributes:
        session_id: 被审查对话 session 的外键(唯一)。
        last_turn: 已写入的最新轮次编号,用于防回退校验。
        crisis_locked_message_id: crisis 粘性接管锚点消息 ID;非空=粘性锁定中,
            session 内不可逆,仅开启新 session 可重置。
        session_notes: 风控视角的跨轮叙事笔记,审查 Agent 按固定骨架整段重写维护
            (话题脉络 / 风险观察 / 情绪走向 / 家长关注点回应)。不注入主 LLM。
        updated_at: 行更新时间。

    注:历史版本在本表持有 ``turn_summaries`` JSONB 字段。M11 拆分后改由独立的
    ``TurnSummary`` 表按 (session_id, turn_number) 存放每轮短摘要,便于 GIN 索引
    与日终专家搜索原子命中单条记录;本类不再存该数据。
    """

    __tablename__ = "rolling_summaries"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    last_turn: Mapped[int] = mapped_column(Integer, nullable=False)
    crisis_locked_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="crisis 粘性接管锚点消息 ID.非空=粘性锁定中,"
        "指向触发 crisis 的首条 ai_msg id;"
        "空=未锁定. session 内不可逆,仅开启新 session 可重置.",
    )
    session_notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="风控视角的跨轮叙事笔记(TEXT),审查 Agent 按固定骨架整段重写维护:"
        "话题脉络 / 风险观察 / 情绪走向 / 家长关注点回应。"
        "供审查自身跨轮复用 + 日终专家生成家长报告;不注入主 LLM,避免风控判断泄漏",
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False, comment="更新时间"
    )


class TurnSummary(BaseMixin, Base):
    """轮次摘要表:每轮对话一条客观中立短摘要。

    由 ``audit.usecase.write_audit_results`` 与 ``audit_records`` 同事务插入,
    取代历史 ``RollingSummary.turn_summaries`` JSONB 字段。拆表动机:
    - 行级 GIN trigram 索引覆盖关键词搜索,无需整 JSON 拉取
    - 原子单条 upsert,审计回放 (session_id, turn_number) 冲突由 unique 守门

    Attributes:
        session_id: 被审查对话 session 的外键,on_delete=CASCADE。
        turn_number: 对话轮次编号,与 ai_turn_counter 对齐。
        summary: 单行客观中立摘要,≤100 字符。
    """

    __tablename__ = "turn_summaries"
    __table_args__ = (
        UniqueConstraint("session_id", "turn_number", name="uq_turn_summaries_session_turn"),
        # GIN trigram 索引:支持 ILIKE '%kw%' 类搜索走索引(非全表扫描)
        Index(
            "idx_ts_summary_trgm",
            "summary",
            postgresql_using="gin",
            postgresql_ops={"summary": "gin_trgm_ops"},
        ),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    turn_number: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
