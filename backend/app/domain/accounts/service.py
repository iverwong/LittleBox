"""accounts 域业务编排服务。

聚合账号与 child 创建、硬删的跨表事务,统一走 `commit_with_redis` 同步纪律,
避免裸 `db.commit()` 造成 DB 与 Redis 不一致。

职责边界:
- 装:跨表事务、跨外部服务(DB + Redis)的事务编排。
- 不装:纯算法换算(仅暴露 `age_to_birth_date` / `birth_date_to_age` 工具函数)。
- 不装:HTTP 协议层(handler 仍负责 Depends 注入与返回类型)。
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enums import UserRole
from app.core.redis import commit_with_redis
from app.core.time import now_utc
from app.domain.accounts.models import (
    AuthToken,
    ChildProfile,
    DataDeletionRequest,
    DeviceToken,
    Family,
    FamilyMember,
    User,
)
from app.domain.accounts.schemas import (
    ChildSummary,
    CreateChildRequest,
    CurrentAccount,
    PutChildProfileRequest,
)
from app.domain.audit.models import AuditRecord, RollingSummary
from app.domain.auth.tokens import revoke_all_active_tokens
from app.domain.chat.models import Message, Session
from app.domain.expert.models import DailyReport
from app.domain.notifications.models import Notification

# ---------------------------------------------------------------------------
# 纯算法工具:年龄 ↔ 出生日期双向换算
# ---------------------------------------------------------------------------


def age_to_birth_date(age: int, ref: date | None = None) -> date:
    """将整数年龄(岁)转换为近似出生日期。

    算法:`ref - age 年同月同日`。当 `ref` 月日为 2-29 且 `ref.year - age`
    非闰年时,`date` 构造会抛 `ValueError`,此函数兜底为 2-28。

    Args:
        age: 整数年龄,合法范围 `[3, 21]`。
        ref: 基准日期,默认 `date.today()`。

    Returns:
        近似出生日期(纯 `date`,无时区)。

    Raises:
        ValueError: `age` 超出 `[3, 21]` 范围。
    """
    if not (3 <= age <= 21):
        raise ValueError(f"age must be in [3, 21], got {age}")

    ref = ref if ref is not None else date.today()
    try:
        return date(ref.year - age, ref.month, ref.day)
    except ValueError:
        # 2-29 落在非闰年时 date() 会抛 ValueError;兜底为前一日 2-28
        return date(ref.year - age, ref.month, ref.day - 1)


def birth_date_to_age(birth_date: date, ref: date | None = None) -> int:
    """将出生日期转换为整数年龄(岁)。

    钳位到 `[3, 21]` 范围,避免极端 `birth_date` 把上游 API 打挂。

    Args:
        birth_date: 出生日期。
        ref: 基准日期,默认 `date.today()`。

    Returns:
        钳位后的整数年龄,范围 `[3, 21]`。
    """
    ref = ref if ref is not None else date.today()
    raw = ref.year - birth_date.year - ((ref.month, ref.day) < (birth_date.month, birth_date.day))
    return max(3, min(21, raw))


# ---------------------------------------------------------------------------
# 跨表事务:创建子账号、硬删子账号
# ---------------------------------------------------------------------------


async def create_child(
    db: AsyncSession,
    redis: Redis,
    *,
    parent: CurrentAccount,
    payload: CreateChildRequest,
) -> ChildSummary:
    """父账号创建一个子账号。

    写入顺序(在同一事务内):
    1. `users` 表插入 `role=child` 的子用户。
    2. `child_profiles` 表插入子账号画像。
    3. `family_members` 表插入成员关联记录。

    并发安全:先对 family 行加 `SELECT ... FOR UPDATE` 行级锁,再统计当前
    child 数量是否已达 `settings.max_children_per_family`,最后写入。
    所有写入通过 `commit_with_redis` 落盘。

    Args:
        db: 数据库会话。
        redis: Redis 客户端,用于随 commit 一并 flush 任何 staged ops。
        parent: 当前父账号上下文,提供 `family_id` 与 `id`。
        payload: 创建子账号的请求体,包含昵称、年龄、性别。

    Returns:
        包含子账号 ID、昵称、出生日期、性别与绑定状态的 `ChildSummary`。
        `is_bound` 硬编码为 `False`(刚创建的 child 尚未生成任何 AuthToken)。

    Raises:
        HTTPException: 家庭下子账号数已达上限时抛出 409。
    """
    # 行级锁:同 family 下并发创建 child 时,锁住 family 行串行化计数
    await db.execute(select(Family).where(Family.id == parent.family_id).with_for_update())
    child_count = (
        await db.execute(
            select(func.count())
            .select_from(User)
            .where(
                User.family_id == parent.family_id,
                User.role == UserRole.child,
            )
        )
    ).scalar_one()
    if child_count >= settings.max_children_per_family:
        raise HTTPException(status_code=409, detail="ChildLimitReached")

    child = User(
        family_id=parent.family_id,
        role=UserRole.child,
        phone=None,
        is_active=True,
    )
    db.add(child)
    await db.flush()

    birth_date = age_to_birth_date(payload.age)  # ref 默认为 today()

    db.add(
        ChildProfile(
            child_user_id=child.id,
            created_by=parent.id,
            birth_date=birth_date,
            gender=payload.gender,
            nickname=payload.nickname,
        )
    )

    db.add(
        FamilyMember(
            family_id=parent.family_id,
            user_id=child.id,
            role=UserRole.child,
            joined_at=now_utc(),
        )
    )

    await commit_with_redis(db, redis)
    return ChildSummary(
        id=child.id,
        nickname=payload.nickname,
        birth_date=birth_date,
        gender=payload.gender,
        is_bound=False,  # 刚创建的 child 必然无 AuthToken
    )


async def hard_delete_child(
    db: AsyncSession,
    *,
    child_user_id: uuid.UUID,
    requested_by: uuid.UUID,
) -> dict[str, int]:
    """硬删指定子账号及其全部关联数据。

    执行步骤(必须严格遵守顺序):
    1. 调用 `revoke_all_active_tokens` 撤销该 child 的所有 token,
       并 stage 清理 Redis 中对应的 token 缓存(CASCADE 触发后 `auth_tokens`
       行消失,token_hash 无法再回查,Redis 清理必须前置)。
    2. 在 CASCADE 触发前对 10 张关联表分别 `SELECT COUNT`,记录各表删除行数。
       其中 `messages` / `audit_records` / `rolling_summaries` 通过 child
       的 session_ids 中介统计。
    3. 执行 `DELETE FROM users` 触发 DB 层 `ON DELETE CASCADE`,清理
       `child_profiles` / `auth_tokens` / `device_tokens` /
       `family_members` / `sessions` / `messages` / `audit_records` /
       `rolling_summaries` / `daily_reports` / `notifications`。
    4. 写入 `DataDeletionRequest` 审计记录,留存合规证据。

    调用方约定:函数返回前**不要** commit;调用方必须在末尾执行
    `await commit_with_redis(db, redis)` 才能落盘。禁用裸 `db.commit()`。
    `family_members` 中 parent 的记录不会被本函数影响(parent 不会被删)。

    Args:
        db: 数据库会话。
        child_user_id: 待硬删的子账号 `User.id`。
        requested_by: 发起删除的家长 `User.id`,用于审计落库。

    Returns:
        `{table_name: deleted_row_count}` 各表删除行数快照。
    """
    # 1) 撤销 token:DB 标 revoked_at + stage 批量 Redis delete
    await revoke_all_active_tokens(db, child_user_id)

    # 2) CASCADE 触发前快照各表行数(供审计落库)
    deleted_tables: dict[str, int] = {}

    deleted_tables["child_profiles"] = (
        await db.execute(
            select(func.count())
            .select_from(ChildProfile)
            .where(ChildProfile.child_user_id == child_user_id)
        )
    ).scalar_one()

    deleted_tables["sessions"] = (
        await db.execute(
            select(func.count()).select_from(Session).where(Session.child_user_id == child_user_id)
        )
    ).scalar_one()

    # messages / audit_records / rolling_summaries 都需要先取到 session_ids 再统计
    session_ids = (
        (await db.execute(select(Session.id).where(Session.child_user_id == child_user_id)))
        .scalars()
        .all()
    )

    deleted_tables["messages"] = (
        (
            await db.execute(
                select(func.count()).select_from(Message).where(Message.session_id.in_(session_ids))
            )
        ).scalar_one()
        if session_ids
        else 0
    )

    deleted_tables["audit_records"] = (
        (
            await db.execute(
                select(func.count())
                .select_from(AuditRecord)
                .where(AuditRecord.session_id.in_(session_ids))
            )
        ).scalar_one()
        if session_ids
        else 0
    )

    deleted_tables["rolling_summaries"] = (
        (
            await db.execute(
                select(func.count())
                .select_from(RollingSummary)
                .where(RollingSummary.session_id.in_(session_ids))
            )
        ).scalar_one()
        if session_ids
        else 0
    )

    deleted_tables["daily_reports"] = (
        await db.execute(
            select(func.count())
            .select_from(DailyReport)
            .where(DailyReport.child_user_id == child_user_id)
        )
    ).scalar_one()

    deleted_tables["notifications"] = (
        await db.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.child_user_id == child_user_id)
        )
    ).scalar_one()

    deleted_tables["auth_tokens"] = (
        await db.execute(
            select(func.count()).select_from(AuthToken).where(AuthToken.user_id == child_user_id)
        )
    ).scalar_one()

    deleted_tables["device_tokens"] = (
        await db.execute(
            select(func.count())
            .select_from(DeviceToken)
            .where(DeviceToken.user_id == child_user_id)
        )
    ).scalar_one()

    deleted_tables["family_members"] = (
        await db.execute(
            select(func.count())
            .select_from(FamilyMember)
            .where(FamilyMember.user_id == child_user_id)
        )
    ).scalar_one()

    # 3) DELETE User 触发 DB 层 CASCADE,清理全部依赖行
    await db.execute(delete(User).where(User.id == child_user_id))

    # 4) 写入审计记录(child_id_snapshot 已无 FK,仅留 UUID 快照)
    db.add(
        DataDeletionRequest(
            requested_by=requested_by,
            child_id_snapshot=child_user_id,
            deleted_tables=deleted_tables,
            reason="parent_request",
        )
    )

    return deleted_tables


# ---------------------------------------------------------------------------
# ChildProfile 查询复用 + snapshot 构造 + 部分更新
# ---------------------------------------------------------------------------


async def load_child_profile(db: AsyncSession, child_user_id: uuid.UUID) -> ChildProfile | None:
    """按 child_user_id 加载 profile(自身 / LLM 路径用,无 family 约束)。

    Args:
        db: 数据库会话。
        child_user_id: 子账号 `User.id`。

    Returns:
        命中的 `ChildProfile`;不存在返回 `None`。
    """
    return (
        await db.execute(select(ChildProfile).where(ChildProfile.child_user_id == child_user_id))
    ).scalar_one_or_none()


async def load_child_profile_in_family(
    db: AsyncSession, *, child_user_id: uuid.UUID, family_id: uuid.UUID
) -> ChildProfile | None:
    """父端访问 child profile 的唯一入口,family 归属焊进同一条 WHERE(防 IDOR)。

    Args:
        db: 数据库会话。
        child_user_id: 目标子账号 `User.id`。
        family_id: 当前父账号所属 family,作为 WHERE 约束的一部分。

    Returns:
        本 family 内命中的 `ChildProfile`;不存在或越权返回 `None`。
    """
    return (
        await db.execute(
            select(ChildProfile)
            .join(User, User.id == ChildProfile.child_user_id)
            .where(
                ChildProfile.child_user_id == child_user_id,
                User.family_id == family_id,
                User.role == UserRole.child,
                User.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


async def update_child_profile(
    db: AsyncSession,
    redis: Redis,
    *,
    parent: CurrentAccount,
    child_user_id: uuid.UUID,
    payload: PutChildProfileRequest,
) -> ChildProfile:
    """父端全量替换子账号配置(PUT 语义)。

    payload 已由 Pydantic 守卫:
    - 必输字段(nickname / birth_date / gender / sensitivity)缺失 → 422
    - `birth_date` 换算整岁越界 [3, 21] → 422
    - `concerns` / `custom_redlines` 空串归一为 None = 清空
    - `nickname` 长度 [1, 12]

    family 归属在 `load_child_profile_in_family` 内焊入 WHERE。

    Args:
        db: 数据库会话。
        redis: Redis 客户端,随 commit 一并 flush staged ops。
        parent: 当前父账号上下文,提供 `family_id`。
        child_user_id: 目标子账号 `User.id`。
        payload: 全量提交请求体。

    Returns:
        更新后的 `ChildProfile`。

    Raises:
        HTTPException: child 不存在或非本 family 时抛 404。
    """
    profile = await load_child_profile_in_family(
        db, child_user_id=child_user_id, family_id=parent.family_id
    )
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "child not found in family")

    # 全量替换:payload 已由 Pydantic 守卫(含 [3,21]、空串归一)
    profile.nickname = payload.nickname
    profile.birth_date = payload.birth_date
    profile.gender = payload.gender  # 已是 Gender 枚举,无需 Gender() 包装
    profile.sensitivity = payload.sensitivity.model_dump()
    profile.concerns = payload.concerns
    profile.custom_redlines = payload.custom_redlines

    await commit_with_redis(db, redis)  # 无 staged Redis op → 等价干净 DB commit
    return profile
