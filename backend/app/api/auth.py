"""auth 路由：login / logout。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_parent
from app.auth.password import verify_password
from app.auth.redis_client import get_redis
from app.auth.redis_ops import RedisOp, commit_with_redis, stage_redis_op
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

LOGIN_PHONE_LIMIT = 5
LOGIN_IP_LIMIT = 20
LOGIN_WINDOW_SECONDS = 60


async def _check_login_limit(redis: Redis, phone: str, ip: str) -> None:
    """检查是否已达限流阈值，是则 raise 429。"""
    phone_key = f"login_fail:phone:{phone}"
    ip_key = f"login_fail:ip:{ip}"
    phone_count = int(await redis.get(phone_key) or 0)
    ip_count = int(await redis.get(ip_key) or 0)
    if phone_count >= LOGIN_PHONE_LIMIT or ip_count >= LOGIN_IP_LIMIT:
        raise HTTPException(429, "too many attempts; try again later")


async def _incr_login_fail(redis: Redis, phone: str, ip: str) -> None:
    """失败一次，递增两个计数器的计数器（pipeline，nx=True TTL）。"""
    phone_key = f"login_fail:phone:{phone}"
    ip_key = f"login_fail:ip:{ip}"
    async with redis.pipeline(transaction=False) as pipe:
        pipe.incr(phone_key)
        pipe.expire(phone_key, LOGIN_WINDOW_SECONDS, nx=True)
        pipe.incr(ip_key)
        pipe.expire(ip_key, LOGIN_WINDOW_SECONDS, nx=True)
        await pipe.execute()


@router.post("/login", response_model=LoginResponse)
async def login(
    request: Request,
    payload: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> LoginResponse:
    """父账号登录：phone + password → opaque token。"""
    client_ip = request.client.host if request.client else "unknown"
    await _check_login_limit(redis, payload.phone, client_ip)

    # 统一 401，不区分账号不存在 / 密码错（防枚举）
    stmt = select(User).where(
        User.phone == payload.phone,
        User.role == UserRole.parent,
        User.is_active.is_(True),
    )
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None or user.password_hash is None:
        await _incr_login_fail(redis, payload.phone, client_ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if not verify_password(user.password_hash, payload.password):
        await _incr_login_fail(redis, payload.phone, client_ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    # 成功：清零两个计数器的计数器（走 staging，随 commit_with_redis 一起 flush）
    stage_redis_op(db, RedisOp(kind="delete", key=f"login_fail:phone:{payload.phone}"))
    stage_redis_op(db, RedisOp(kind="delete", key=f"login_fail:ip:{client_ip}"))

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
