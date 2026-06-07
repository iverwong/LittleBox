"""Accounts 域业务编排服务。

聚合账号 / child 创建与硬删的跨表事务,统一走 commit_with_redis 同步纪律
(避免裸 db.commit() 造成 DB / Redis 不一致)。

边界(D-1 + D-2):
- 装:跨表事务 / 跨外部服务(DB + Redis)的事务编排
- 不装:纯算法(age 换算只暴露 age_to_birth_date / birth_date_to_age 工具)
- 不装:HTTP 协议层(handler 仍负责 Depends 注入与返回类型)
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import revoke_all_active_tokens
from app.core.config import settings
from app.core.redis import commit_with_redis
from app.domain.accounts.schemas import (
    ChildSummary,
    CreateChildRequest,
    CurrentAccount,
)
from app.models.accounts import (
    AuthToken,
    ChildProfile,
    DeviceToken,
    Family,
    FamilyMember,
    User,
)
from app.models.audit import AuditRecord, RollingSummary
from app.models.chat import Message, Session
from app.models.enums import UserRole
from app.models.parent import DailyReport, DataDeletionRequest, Notification

# ---------------------------------------------------------------------------
# 纯算法工具(M4.8 B2)
# ---------------------------------------------------------------------------


def age_to_birth_date(age: int, ref: date | None = None) -> date:
    """将 age(岁)转换为近似 birth_date。

    算法:ref - age 年同月同日;闰年 2-29 在非闰年触发 ValueError,兜底为前一日。

    Args:
        age: 年龄,必须在 [3, 21] 范围内。
        ref: 基准日期,默认为 date.today()。

    Returns:
        近似出生日期。

    Raises:
        ValueError: age 不在 [3, 21] 范围内。
    """
    if not (3 <= age <= 21):
        raise ValueError(f"age must be in [3, 21], got {age}")

    ref = ref if ref is not None else date.today()
    try:
        return date(ref.year - age, ref.month, ref.day)
    except ValueError:
        # 2-29 in non-leap year → fall back to 2-28
        return date(ref.year - age, ref.month, ref.day - 1)


def birth_date_to_age(birth_date: date, ref: date | None = None) -> int:
    """将 birth_date 转换为 age(岁)。

    钳位到 [3, 21] 范围,避免极端 birth_date 把 API 打挂。

    Args:
        birth_date: 出生日期。
        ref: 基准日期,默认为 date.today()。

    Returns:
        3-21 之间的年龄。
    """
    ref = ref if ref is not None else date.today()
    raw = ref.year - birth_date.year - ((ref.month, ref.day) < (birth_date.month, birth_date.day))
    return max(3, min(21, raw))


# ---------------------------------------------------------------------------
# 跨表事务(M4.8 B3 / M4.8 B6)
# ---------------------------------------------------------------------------


async def create_child(
    db: AsyncSession,
    redis: Redis,
    *,
    parent: CurrentAccount,
    payload: CreateChildRequest,
) -> ChildSummary:
    """父账号创建一个子账号:users(role=child) + child_profiles + family_members。

    family 行级锁防并发超限,所有写入在同一事务,走 commit_with_redis 落盘。
    """
    # M5 hotfix: family child count limit — SELECT FOR UPDATE + COUNT within same tx
    # Acquire row-level lock on the family row before counting
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
            joined_at=datetime.now(timezone.utc),
        )
    )

    await commit_with_redis(db, redis)
    return ChildSummary(
        id=child.id,
        nickname=payload.nickname,
        birth_date=birth_date,
        gender=payload.gender,
        is_bound=False,  # 硬编码:刚创建的 child 必然无 AuthToken
    )


async def hard_delete_child(
    db: AsyncSession,
    *,
    child_user_id: uuid.UUID,
    requested_by: uuid.UUID,
) -> dict[str, int]:
    """硬删 child 账号及其全部关联数据。

    三步顺序(必须遵守):
      ① stage Redis auth 缓存清理(CASCADE 后 auth_tokens 行消失,token_hash 取不回)
      ② SELECT COUNT 各表(CASCADE 前快照)
      ③ DELETE User 触发 DB 层 CASCADE
      ④ 写入 DataDeletionRequest

    调用方必须用 commit_with_redis(db, redis) 提交。禁用裸 db.commit()。
    不处理 family_members 中 parent 的记录(parent 不会被删)。
    """
    # ① stage Redis auth 缓存清理
    await revoke_all_active_tokens(db, child_user_id)

    # ② SELECT COUNT 各表(CASCADE 前)
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

    # messages/audit_records/rolling_summaries 以 session_ids 为中介
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

    # ③ DELETE User 触发 DB 层 CASCADE
    await db.execute(delete(User).where(User.id == child_user_id))

    # ④ 写入审计记录
    db.add(
        DataDeletionRequest(
            requested_by=requested_by,
            child_id_snapshot=child_user_id,
            deleted_tables=deleted_tables,
            reason="parent_request",
        )
    )

    return deleted_tables
