"""auth 路由：login / logout。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_parent
from app.auth.password import verify_password
from app.auth.redis_client import get_redis
from app.auth.redis_ops import commit_with_redis
from app.auth.tokens import (
    issue_token,
    revoke_all_active_tokens,
    revoke_token,
)
from app.db import get_db
from app.models.accounts import User
from app.models.enums import UserRole
from app.schemas.accounts import AccountOut, LoginRequest, LoginResponse

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> LoginResponse:
    """父账号登录：phone + password → opaque token。"""
    # 统一 401，不区分账号不存在 / 密码错（防枚举）
    stmt = select(User).where(
        User.phone == payload.phone,
        User.role == UserRole.parent,
        User.is_active.is_(True),
    )
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None or user.password_hash is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if not verify_password(user.password_hash, payload.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    # 新设备登录吊销该 parent 所有活跃 token
    await revoke_all_active_tokens(db, user.id)
    token = await issue_token(
        db,
        user_id=user.id,
        role=user.role,
        family_id=user.family_id,
        device_id=payload.device_id,
        ttl_days=7,
    )
    await commit_with_redis(db, redis)
    return LoginResponse(
        token=token,
        account=AccountOut(
            id=user.id,
            role=user.role,
            family_id=user.family_id,
            phone=user.phone,
            is_active=user.is_active,
        ),
    )


@router.post("/logout", status_code=204)
async def logout(
    authorization: Annotated[str, Header()],
    current: Annotated[object, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> None:
    """主动下线当前父账号 token。限 parent。"""
    token = authorization.split(" ", 1)[1].strip()
    await revoke_token(db, token)
    await commit_with_redis(db, redis)
