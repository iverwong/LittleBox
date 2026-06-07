"""notifications 域 ORM(1 张表:Notification)。

M10+ 真实通知推送真实化时,本表 schema 可能扩展(payload 强类型化、
delivery_status 字段等);Phase 6 仅做物理迁出,不改 schema。
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, BaseMixin
from app.core.enums import NotificationType


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
