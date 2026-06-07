"""accounts 域 ORM(7 张表)。

D-1 边界:本模块 import app.core.db(零业务依赖叶子),不 import
其他 domain/*/models。所有跨表 FK 用字符串("users.id" / "families.id"
等),跨域 relationship 也用字符串目标,不依赖 Python 层级 import。
alembic 看到完整 13 张表由 app.models.__init__ 聚合点副作用保证。
"""

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, BaseMixin
from app.core.enums import DevicePlatform, Gender, SubTier, UserRole


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
    password_hash: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="argon2id 哈希，CLI / 登录时写入",
    )
    admin_note: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="运维备注，CLI --note 写入",
    )

    __table_args__ = (
        Index(
            "idx_users_phone_parent_active",
            "phone",
            postgresql_where=text("role = 'parent' AND is_active = true"),
        ),
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
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="创建者（审计用途；权限通过 family_id 控制）",
    )
    birth_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        comment="家长输入 age，存 today - age years",
    )
    gender: Mapped[Gender] = mapped_column(nullable=False)
    nickname: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="家长设置的子女昵称，B1 占位，B3 替换为 payload.nickname",
    )
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
        ForeignKey("users.id", ondelete="CASCADE"),
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
    device_id: Mapped[str] = mapped_column(String(255), nullable=False)
    device_info: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="审计用：{ua, ip, platform}",
    )

    __table_args__ = (
        Index("ix_auth_tokens_token_hash", "token_hash", unique=True),
        Index(
            "ix_auth_tokens_user_id_active",
            "user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class DeviceToken(BaseMixin, Base):
    """设备推送令牌，用于向父账号设备发送通知。"""

    __tablename__ = "device_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
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


class FamilyMember(BaseMixin, Base):
    """家庭成员关联表。MVP 同时维护 users.family_id 冗余字段；此表承接「一家 N 父」扩展。"""

    __tablename__ = "family_members"

    family_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("families.id"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[UserRole] = mapped_column(nullable=False)
    joined_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
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
