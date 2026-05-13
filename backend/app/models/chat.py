import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BaseMixin
from app.models.enums import InterventionType, MessageRole, MessageStatus, SessionStatus


class Session(BaseMixin, Base):
    """对话会话。每个子账号可拥有多个会话。"""

    __tablename__ = "sessions"
    __table_args__ = (Index("idx_sessions_child", "child_user_id", "status"),)

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

    # relationships
    messages: Mapped[list["Message"]] = relationship(
        back_populates="session",
        order_by="Message.created_at",
    )


class Message(BaseMixin, Base):
    """对话消息。role 使用 human/ai 对齐 LangChain 消息类型。"""

    __tablename__ = "messages"
    __table_args__ = (Index("idx_messages_session", "session_id", "created_at"),)

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

    # relationships
    session: Mapped["Session"] = relationship(back_populates="messages")
