"""notifications 域 ORM 模型。

当前包含 1 张表:`Notification`,用于记录推送至家长的通知。
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
    """家长通知(危机实时推送 / 日常摘要)。

    Attributes:
        id: 主键 UUID(继承自 BaseMixin)。
        created_at: 记录创建时间(继承自 BaseMixin)。
        parent_user_id: 接收通知的家长 user.id。
        child_user_id: 关联的孩子 user.id(可空)。child 删除时 CASCADE 清空,
            系统级通知此处为 NULL。
        type: 通知类型枚举(crisis / daily_summary)。
        payload: 通知载荷 JSON(可空)。代码层不约束 schema,消费方按 type 解构。
        sent_at: 实际发送时间(可空,未发送则为 NULL)。
        read_at: 家长已读时间(可空,未读则为 NULL)。
    """

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
        comment="关联的 child(可选);child 删除时 CASCADE 清空,系统通知为 NULL",
    )
    type: Mapped[NotificationType] = mapped_column(nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="代码层不约束 schema,消费方按 type 解构",
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
    read_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )
