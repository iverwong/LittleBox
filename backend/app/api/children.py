"""children 路由：创建 child / 生成 bind_token / 查询绑定状态 / 吊销 child tokens。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.bind import (
    BIND_KEY_PREFIX,
    BIND_RESULT_KEY_PREFIX,
    issue_bind_token,
)
from app.auth.deps import require_parent
from app.auth.redis_client import get_redis
from app.auth.redis_ops import commit_with_redis
from app.auth.tokens import revoke_all_active_tokens
from app.db import get_db
from app.models.accounts import ChildProfile, User
from app.models.enums import UserRole
from app.schemas.accounts import (
    AccountOut,
    BindTokenResponse,
    BindTokenStatusOut,
    CreateChildRequest,
    CurrentAccount,
)

router = APIRouter(prefix="/api/v1", tags=["children"])


@router.post("/children", response_model=AccountOut, status_code=201)
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


@router.post("/children/{child_user_id}/bind-token", response_model=BindTokenResponse)
async def create_bind_token(
    child_user_id: uuid.UUID,
    parent: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> BindTokenResponse:
    """为指定子账号生成一次性绑定 token。"""
    # 家庭边界检查
    stmt = select(User).where(
        User.id == child_user_id,
        User.role == UserRole.child,
        User.family_id == parent.family_id,
        User.is_active.is_(True),
    )
    child = (await db.execute(stmt)).scalar_one_or_none()
    if child is None:
        raise HTTPException(404, "child not found in family")
    token = await issue_bind_token(
        redis, parent_user_id=parent.id, child_user_id=child.id,
    )
    return BindTokenResponse(bind_token=token)


@router.get("/bind-tokens/{bind_token}/status", response_model=BindTokenStatusOut)
async def get_bind_token_status(
    bind_token: str,
    redis: Annotated[Redis, Depends(get_redis)],
) -> BindTokenStatusOut:
    """轮询 bind_result:{bind_token} Redis key；无结果再看 bind_token 本身是否活着。

    **不鉴权**：bind_token 本身是一次性机密凭证（5min TTL + 16 字节 urlsafe 随机），
    持有即算父端——与「生成 bind_token」端点的 require_parent 对称闭合。
    """
    # 1) 已兑换 → status=bound
    result_raw = await redis.get(f"{BIND_RESULT_KEY_PREFIX}{bind_token}")
    if result_raw is not None:
        data = json.loads(result_raw)
        return BindTokenStatusOut(
            status="bound",
            child_user_id=uuid.UUID(data["child_user_id"]),
            bound_at=datetime.fromisoformat(data["bound_at"]),
        )
    # 2) 未兑换但 bind_token 还活着 → status=pending
    if await redis.exists(f"{BIND_KEY_PREFIX}{bind_token}"):
        return BindTokenStatusOut(status="pending")
    # 3) 两者皆无 → bind_token 已过期且未兑换（或根本不存在）
    raise HTTPException(404, "bind token not found or expired")


@router.post("/children/{child_user_id}/revoke-tokens", status_code=204)
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
