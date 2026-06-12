"""chat 域 ORM(2 张表:Session / Message)。

D-1 边界:同 domain/accounts/models.py,零跨域 Python import。
relationship 用字符串目标("Message" / "Session"),由 mapper 配置时
延迟解析,顺序无关。
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, BaseMixin
from app.core.enums import InterventionType, MessageRole, MessageStatus, SessionStatus


class Session(BaseMixin, Base):
    """对话会话。每个子账号可拥有多个会话。"""

    __tablename__ = "sessions"
    # M6 partial 索引（keyset 分页 + WHERE status='active' 读路径优化）。
    # 名字 + 列序 + WHERE 子句与迁移 a77f2c1e8b34 创建的 PG 索引严格一致，
    # 保证 alembic check 不报漂移。ORM 与 DB 的 DDL 是同一份事实的两个表示，
    # 任何对其中一边的改动都必须同步另一边。
    __table_args__ = (
        Index(
            "idx_sessions_child_active_lastactive",
            "child_user_id",
            text("last_active_at DESC"),
            text("id DESC"),
            postgresql_where=text("status = 'active'"),
        ),
    )

    child_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    status: Mapped[SessionStatus] = mapped_column(
        default=SessionStatus.active,
        server_default="active",
        nullable=False,
    )
    last_active_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    context_size_tokens: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="末轮 LLM usage input_tokens + output_tokens 快照",
    )
    needs_compression: Mapped[bool] = mapped_column(
        default=False,
        server_default="false",
        nullable=False,
        comment="阈值命中 → True，下一轮 user 到达时阻塞压缩",
    )
    ai_turn_counter: Mapped[int] = mapped_column(
        default=0,
        server_default="0",
        nullable=False,
        comment="LLM AI 回复累积轮次；persist_ai_turn 同事务 +1",
    )

    # relationships
    messages: Mapped[list["Message"]] = relationship(
        back_populates="session",
        order_by="Message.created_at",
    )


class Message(BaseMixin, Base):
    """对话消息。role 使用 human/ai 对齐 LangChain 消息类型。"""

    __tablename__ = "messages"
    # M6 partial 索引（与 Session 一致：keyset 分页 + WHERE status='active'）。
    # 名字 + 列序 + WHERE 子句与迁移 a77f2c1e8b34 创建的 PG 索引严格一致。
    __table_args__ = (
        Index(
            "idx_messages_session_active_created",
            "session_id",
            text("created_at DESC"),
            text("id DESC"),
            postgresql_where=text("status = 'active'"),
        ),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[MessageRole] = mapped_column(nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    intervention_type: Mapped[Optional[InterventionType]] = mapped_column(
        nullable=True,
        comment="null=正常回复, crisis=危机接管, redline=红线接管, guided=二级注入后回复",
    )
    status: Mapped[MessageStatus] = mapped_column(
        default=MessageStatus.active,
        server_default="active",
        nullable=False,
    )
    finish_reason: Mapped[Optional[str]] = mapped_column(
        String(length=50),
        nullable=True,
        comment="LLM finish_reason: stop/length/content_filter/user_stopped 等",
    )
    turn_number: Mapped[int] = mapped_column(
        default=0,
        server_default="0",
        nullable=False,
        comment="对话轮次编号。human/ai 同轮共享同号；summary/discarded 行保持 0。"
        "由 Step 3 commit①/commit② 与 backfill SQL 共同维护",
    )

    # relationships
    session: Mapped["Session"] = relationship(back_populates="messages")
