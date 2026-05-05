"""children 路由：创建 child / 吊销 child tokens / 列表查询 / 删除 child。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from redis.asyncio import Redis
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_parent
from app.auth.redis_client import get_redis
from app.auth.redis_ops import commit_with_redis
from app.auth.tokens import revoke_all_active_tokens
from app.config import settings
from app.db import get_db
from app.models.accounts import AuthToken, ChildProfile, Family, FamilyMember, User
from app.models.enums import UserRole
from app.schemas.accounts import CurrentAccount
from app.schemas.children import ChildSummary, CreateChildRequest, ListChildrenResponse
from app.services.age_converter import age_to_birth_date
from app.services.child_deletion import hard_delete_child

router = APIRouter(prefix="/api/v1/children", tags=["children"])


@router.post("", response_model=ChildSummary, status_code=201)
async def create_child(
    payload: CreateChildRequest,
    parent: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> ChildSummary:
    """父账号创建一个子账号：users(role=child) + child_profiles + family_members。"""
    # M5 hotfix: family child count limit — SELECT FOR UPDATE + COUNT within same tx
    # Acquire row-level lock on the family row before counting
    await db.execute(
        select(Family)
        .where(Family.id == parent.family_id)
        .with_for_update()
    )
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

    db.add(ChildProfile(
        child_user_id=child.id,
        created_by=parent.id,
        birth_date=birth_date,
        gender=payload.gender,
        nickname=payload.nickname,
    ))

    db.add(FamilyMember(
        family_id=parent.family_id,
        user_id=child.id,
        role=UserRole.child,
        joined_at=datetime.now(timezone.utc),
    ))

    await commit_with_redis(db, redis)
    return ChildSummary(
        id=child.id,
        nickname=payload.nickname,
        birth_date=birth_date,
        gender=payload.gender,
        is_bound=False,  # 硬编码：刚创建的 child 必然无 AuthToken
    )


@router.get("", response_model=ListChildrenResponse)
async def list_children(
    parent: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ListChildrenResponse:
    """父账号查询本 family 下所有 child。is_bound 通过 EXISTS 动态构造，不落库。"""
    # TODO: 后续若启用有限期 token，需在此补判 expires_at > now()
    is_bound_sub = (
        exists()
        .where(AuthToken.user_id == User.id, AuthToken.revoked_at.is_(None))
        .correlate(User)
        .label("is_bound")
    )

    stmt = (
        select(
            User.id,
            ChildProfile.nickname,
            ChildProfile.birth_date,
            ChildProfile.gender,
            ChildProfile.created_at,
            is_bound_sub,
        )
        .outerjoin(ChildProfile, ChildProfile.child_user_id == User.id)
        .where(User.family_id == parent.family_id, User.role == UserRole.child)
        .order_by(ChildProfile.created_at, ChildProfile.id)
    )
    rows = (await db.execute(stmt)).fetchall()
    return ListChildrenResponse(
        children=[
            ChildSummary(
                id=row.id,
                nickname=row.nickname,
                birth_date=row.birth_date,
                gender=row.gender,
                is_bound=row.is_bound,
            )
            for row in rows
        ]
    )


@router.post("/{child_user_id}/revoke-tokens", status_code=204)
async def revoke_child_tokens(
    child_user_id: uuid.UUID,
    parent: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> None:
    """吊销指定 child 的全部活跃 token。家庭边界：parent 只能下线本 family 的 child。"""
    stmt = select(User).where(
        User.id == child_user_id,
        User.role == UserRole.child,
        User.family_id == parent.family_id,
        User.is_active.is_(True),
    )
    child = (await db.execute(stmt)).scalar_one_or_none()
    if child is None:
        raise HTTPException(404, "child not found in family")
    await revoke_all_active_tokens(db, child.id)
    await commit_with_redis(db, redis)


@router.delete("/{child_user_id}", status_code=204)
async def delete_child(
    child_user_id: uuid.UUID,
    parent: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> None:
    """硬删 child 账号及全部关联数据（DB CASCADE + Redis 缓存清理 + 审计写入）。

    错误码矩阵：
    - 401：未登录（依赖链 require_parent）
    - 403：非 parent 角色（require_parent 抛出）
    - 404：目标 child 不存在 / 非本 family / role 不是 child（不暴露存在性）
    - 204：成功
    """
    stmt = select(User).where(
        User.id == child_user_id,
        User.role == UserRole.child,
        User.family_id == parent.family_id,
        User.is_active.is_(True),
    )
    child = (await db.execute(stmt)).scalar_one_or_none()
    if child is None:
        raise HTTPException(404, "child not found in family")

    await hard_delete_child(db, child_user_id=child_user_id, requested_by=parent.id)
    await commit_with_redis(db, redis)
