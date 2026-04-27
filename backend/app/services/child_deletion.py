"""child hard-delete 服务：级联 CASCADE + Redis 缓存清理 + 审计写入。"""
from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import revoke_all_active_tokens
from app.models.accounts import (
    AuthToken,
    ChildProfile,
    DeviceToken,
    FamilyMember,
    User,
)
from app.models.audit import AuditRecord, RollingSummary
from app.models.chat import Message, Session
from app.models.parent import DailyReport, DataDeletionRequest, Notification


async def hard_delete_child(
    db: AsyncSession,
    *,
    child_user_id: uuid.UUID,
    requested_by: uuid.UUID,
) -> dict[str, int]:
    """硬删 child 账号及其全部关联数据。

    三步顺序（必须遵守）：
      ① stage Redis auth 缓存清理（CASCADE 后 auth_tokens 行消失，token_hash 取不回）
      ② SELECT COUNT 各表（CASCADE 前快照）
      ③ DELETE User 触发 DB 层 CASCADE
      ④ 写入 DataDeletionRequest

    调用方必须用 commit_with_redis(db, redis) 提交。禁用裸 db.commit()。
    不处理 family_members 中 parent 的记录（parent 不会被删）。
    """
    # ① stage Redis auth 缓存清理
    await revoke_all_active_tokens(db, child_user_id)

    # ② SELECT COUNT 各表（CASCADE 前）
    deleted_tables: dict[str, int] = {}

    deleted_tables["child_profiles"] = (
        await db.execute(
            select(func.count()).select_from(ChildProfile).where(
                ChildProfile.child_user_id == child_user_id
            )
        )
    ).scalar_one()

    deleted_tables["sessions"] = (
        await db.execute(
            select(func.count()).select_from(Session).where(
                Session.child_user_id == child_user_id
            )
        )
    ).scalar_one()

    # messages/audit_records/rolling_summaries 以 session_ids 为中介
    session_ids = (
        await db.execute(
            select(Session.id).where(Session.child_user_id == child_user_id)
        )
    ).scalars().all()

    deleted_tables["messages"] = (
        await db.execute(
            select(func.count()).select_from(Message).where(
                Message.session_id.in_(session_ids)
            )
        )
    ).scalar_one() if session_ids else 0

    deleted_tables["audit_records"] = (
        await db.execute(
            select(func.count()).select_from(AuditRecord).where(
                AuditRecord.session_id.in_(session_ids)
            )
        )
    ).scalar_one() if session_ids else 0

    deleted_tables["rolling_summaries"] = (
        await db.execute(
            select(func.count()).select_from(RollingSummary).where(
                RollingSummary.session_id.in_(session_ids)
            )
        )
    ).scalar_one() if session_ids else 0

    deleted_tables["daily_reports"] = (
        await db.execute(
            select(func.count()).select_from(DailyReport).where(
                DailyReport.child_user_id == child_user_id
            )
        )
    ).scalar_one()

    deleted_tables["notifications"] = (
        await db.execute(
            select(func.count()).select_from(Notification).where(
                Notification.child_user_id == child_user_id
            )
        )
    ).scalar_one()

    deleted_tables["auth_tokens"] = (
        await db.execute(
            select(func.count()).select_from(AuthToken).where(
                AuthToken.user_id == child_user_id
            )
        )
    ).scalar_one()

    deleted_tables["device_tokens"] = (
        await db.execute(
            select(func.count()).select_from(DeviceToken).where(
                DeviceToken.user_id == child_user_id
            )
        )
    ).scalar_one()

    deleted_tables["family_members"] = (
        await db.execute(
            select(func.count()).select_from(FamilyMember).where(
                FamilyMember.user_id == child_user_id
            )
        )
    ).scalar_one()

    # ③ DELETE User 触发 DB 层 CASCADE
    await db.execute(delete(User).where(User.id == child_user_id))

    # ④ 写入审计记录
    db.add(DataDeletionRequest(
        requested_by=requested_by,
        child_id_snapshot=child_user_id,
        deleted_tables=deleted_tables,
        reason="parent_request",
    ))

    return deleted_tables
