"""accounts 域 ORM(7 张表)。

边界:
- 仅依赖 `app.core.db` 与 `app.core.enums`,不引入其他 domain 的 ORM。
- 跨表 FK 用字符串("users.id" / "families.id" 等),跨域 relationship
  也用字符串目标,不依赖 Python 层 import 顺序。
- alembic 通过 `app.core.models` 聚合点看到全部 13 张表;漏一处 import
  即 alembic check 产 DROP。
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
    """家庭单元。

    Attributes:
        sub_tier: 订阅档位,默认免费。
        users: 反向引用本家庭下的全部用户。
    """

    __tablename__ = "families"

    sub_tier: Mapped[SubTier] = mapped_column(
        default=SubTier.free,
        server_default=SubTier.free,
        nullable=False,
    )

    # relationships
    users: Mapped[list["User"]] = relationship(back_populates="family")


class User(BaseMixin, Base):
    """用户账号。

    父账号与子账号共用一张表,通过 `role` 字段区分。

    Attributes:
        family_id: 所属家庭 ID(必填)。
        role: 用户角色,父或子。
        phone: 手机号,仅父账号使用。
        is_active: 是否启用,默认 true。
        consent_at: 监护人同意时间,仅 parent 写入。
        consent_version: 同意的隐私政策版本。
        password_hash: argon2id 哈希,登录与 CLI 创建父账号时写入。
        admin_note: 运维备注,CLI `--note` 写入。
        family: 反向引用所属家庭。
        child_profile: 子账号画像(仅 role=child 时存在,一对一)。
    """

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
        comment="监护人同意时间(仅 parent)",
    )
    consent_version: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="同意的隐私政策版本",
    )
    password_hash: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="argon2id 哈希,登录与 CLI 创建父账号时写入",
    )
    admin_note: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="运维备注,CLI --note 写入",
    )

    # 父账号登录查询热路径:phone + 角色 + 启用三合一索引
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
    """子账号画像配置,由家长创建和管理。

    Attributes:
        child_user_id: 关联的子用户 ID,一对一,唯一。
        created_by: 创建者父账号 ID(审计用;权限按 family_id 控制)。
        birth_date: 出生日期,由 `age_to_birth_date` 反推存库。
        gender: 性别,枚举值。
        nickname: 家长设置的子女昵称,长度 1-12。
        concerns: 家长自然语言描述的关注点,注入审查提示词与日终专家提示词。
        sensitivity: 6 维度 0-9 的 JSON 字典,默认 5。
        custom_redlines: 家长自然语言描述的红线话题,审查 Agent 作为 0/1 判定条件,
            命中后触发温和转移并通知家长。
        child_user: 反向引用关联的子账号。
    """

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
        comment="创建者(审计用途;权限通过 family_id 控制)",
    )
    birth_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        comment="家长输入 age,存 today - age years",
    )
    gender: Mapped[Gender] = mapped_column(nullable=False)
    nickname: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        comment="家长设置的子女昵称,长度 1-12",
    )
    concerns: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="家长自然语言描述的关注点,注入审查提示词与日终专家提示词",
    )
    sensitivity: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="SensitivityConfig JSON,6 维度 0-9(默认 5)",
    )
    custom_redlines: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="家长自然语言描述的红线话题,审查 Agent 作为 0/1 判定条件,"
        "命中触发温和转移并通知家长",
    )

    # relationships
    child_user: Mapped["User"] = relationship(
        back_populates="child_profile",
        foreign_keys="[ChildProfile.child_user_id]",
    )


class AuthToken(BaseMixin, Base):
    """登录令牌。

    Attributes:
        user_id: 所属用户 ID,删除用户时级联清理。
        token_hash: token 的 sha256 摘要,非明文。
        expires_at: 过期时间,NULL 表示永不过期(子账号固定为 NULL)。
        revoked_at: 撤销时间,父账号主动解绑时写入。
        device_id: 设备标识,与请求头 `X-Device-Id` 绑定校验。
        device_info: 审计信息 `{ua, ip, platform}` JSON。
    """

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
        comment="NULL = 永不过期(子账号)",
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
        comment="审计用:{ua, ip, platform}",
    )

    # token_hash 唯一索引 + 未撤销用户的 user_id 部分索引(支持 token 解析热路径)
    __table_args__ = (
        Index("ix_auth_tokens_token_hash", "token_hash", unique=True),
        Index(
            "ix_auth_tokens_user_id_active",
            "user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class DeviceToken(BaseMixin, Base):
    """设备推送令牌,用于向父账号设备发送通知。

    Attributes:
        user_id: 所属用户 ID,删除用户时级联清理。
        platform: 设备平台,ios 或 android。
        token: 推送服务(APNs / FCM)颁发的设备 token。
        updated_at: 最近一次更新时间。
    """

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
    """家庭成员关联表。

    `users.family_id` 已承载 MVP 所需的一户一父 + 多子关系;此表保留用于
    "一家多父"等成员关系扩展。

    Attributes:
        family_id: 所属家庭 ID。
        user_id: 关联用户 ID,删除用户时级联清理。
        role: 成员角色。
        joined_at: 加入时间。
    """

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
    """数据删除请求审计记录(合规)。

    仅记录已经实际完成的硬删,落库前由 `hard_delete_child` 汇总各表删除行数。

    Attributes:
        requested_by: 发起删除的家长 ID,保留 FK(parent 不会被删)。
        child_id_snapshot: 被删 child 的 user.id 快照;该 child 已 CASCADE
            物理删除,因此此处不设 FK,仅留 UUID。
        deleted_tables: `{table: count}` 各表删除行数。
        reason: 触发原因,固定为 `parent_request`。
    """

    __tablename__ = "data_deletion_requests"

    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),  # 保留 FK:parent 不会被删
        nullable=False,
        comment="发起删除的家长",
    )
    child_id_snapshot: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),  # 无 FK:child 已 CASCADE 删除,仅留 UUID 快照
        nullable=False,
        comment="被删 child 的 user.id(快照)",
    )
    deleted_tables: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="{table: count} 各表删除行数",
    )
    reason: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="触发原因,固定 parent_request",
    )
