"""children 路由：创建 child / 吊销 child tokens。"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_parent
from app.auth.redis_client import get_redis
from app.auth.redis_ops import commit_with_redis
from app.auth.tokens import revoke_all_active_tokens
from app.db import get_db
from app.models.accounts import ChildProfile, User
from app.models.enums import UserRole
from app.schemas.accounts import (
    AccountOut,
    CreateChildRequest,
    CurrentAccount,
)

router = APIRouter(prefix="/api/v1/children", tags=["children"])


@router.post("", response_model=AccountOut, status_code=201)
async def create_child(
    payload: CreateChildRequest,
    parent: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> AccountOut:
    """父账号创建一个子账号：users(role=child) + child_profiles。"""
    child = User(
        family_id=parent.family_id,
        role=UserRole.child,
        phone=None,
        is_active=True,
    )
    db.add(child)
    await db.flush()

    db.add(ChildProfile(
        child_user_id=child.id,
        created_by=parent.id,
        birth_date=payload.birth_date,
        gender=payload.gender,
    ))

    # 无 Redis ops 也走统一入口
    await commit_with_redis(db, redis)
    return AccountOut(
        id=child.id,
        role=child.role,
        family_id=child.family_id,
        phone=None,
        is_active=True,
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
