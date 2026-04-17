import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, ForeignKey, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BaseMixin
from app.models.enums import DevicePlatform, Gender, SubTier, UserRole


class Family(BaseMixin, Base):
    """家庭单元。注册父账号时自动创建，前期用户不感知。"""

    __tablename__ = "families"

    sub_tier: Mapped[SubTier] = mapped_column(
        default=SubTier.free,
        server_default="free",
        nullable=False,
    )

    # relationships
    users: Mapped[list["User"]] = relationship(back_populates="family")


class User(BaseMixin, Base):
    """用户账号，父/子共用一张表，通过 role 区分。"""

    __tablename__ = "users"

    family_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("families.id"),
        nullable=False,
    )
    role: Mapped[UserRole] = mapped_column(nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
        comment="仅父账号",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        server_default=text("true"),
        nullable=False,
    )
    consent_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="监护人同意时间（仅 parent）",
    )
    consent_version: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="同意的隐私政策版本",
    )

    # relationships
    family: Mapped["Family"] = relationship(back_populates="users")
    child_profile: Mapped[Optional["ChildProfile"]] = relationship(
        back_populates="child_user",
        uselist=False,
        foreign_keys="[ChildProfile.child_user_id]",
    )


class ChildProfile(BaseMixin, Base):
    """子账号画像配置，由家长创建和管理。"""

    __tablename__ = "child_profiles"

    child_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        unique=True,
        nullable=False,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        comment="创建者（审计用途；权限通过 family_id 控制）",
    )
    birth_date: Mapped[Optional[date]] = mapped_column(
        Date,
        nullable=True,
        comment="家长输入 age，存近似生日 today - age years",
    )
    gender: Mapped[Optional[Gender]] = mapped_column(nullable=True)
    concerns: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="家长自然语言描述的关注点，注入审查提示词和日终专家提示词",
    )
    sensitivity: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="SensitivityConfig JSON，7 维度 0-9（默认 5）",
    )
    custom_redlines: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="家长自然语言描述的红线话题，审查 Agent 作为 0/1 判定条件，"
        "命中触发三级接管（温和转移）+ 通知家长",
    )

    # relationships
    child_user: Mapped["User"] = relationship(
        back_populates="child_profile",
        foreign_keys="[ChildProfile.child_user_id]",
    )


class AuthToken(BaseMixin, Base):
    """登录令牌。子账号 expires_at=NULL 表示永不过期。"""

    __tablename__ = "auth_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="NULL = 永不过期（子账号）",
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="父账号解绑时写入",
    )
    device_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class DeviceToken(BaseMixin, Base):
    """设备推送令牌，用于向父账号设备发送通知。"""

    __tablename__ = "device_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    platform: Mapped[DevicePlatform] = mapped_column(nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
