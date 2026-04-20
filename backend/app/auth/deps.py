"""FastAPI Depends：get_current_account / require_parent / require_child。"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.redis_client import get_redis
from app.auth.redis_ops import commit_with_redis
from app.auth.tokens import (
    needs_roll,
    resolve_token,
    revoke_token,
    roll_token_expiry,
    token_hash,
)
from app.db import get_db  # type: ignore[attr-defined]
from app.models.enums import UserRole
from app.schemas.accounts import CurrentAccount


async def get_current_account(
    authorization: Annotated[str | None, Header()] = None,
    x_device_id: Annotated[str | None, Header(alias="X-Device-Id")] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = ...,  # type: ignore[assignment]
    redis: Annotated[Redis, Depends(get_redis)] = ...,  # type: ignore[assignment]
) -> CurrentAccount:
    """从 Authorization: Bearer 解析 token 并返回当前账号。

    流程：
    1. 解析 Bearer token
    2. resolve_token 查 Redis → miss 查 DB → 回填 Redis
    3. 比对 X-Device-Id header 与 payload.device_id，不匹配则吊销并 401
    4. 若 needs_roll(parent token 今日首次续期) 则调用 roll_token_expiry + commit_with_redis
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    payload = await resolve_token(db, redis, token)
    if payload is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")

    # 设备绑定：比对 X-Device-Id header 与 auth_tokens.device_id
    # 不匹配 → 立即吊销 token + 401（fail-closed，防止 token 泄漏后换设备继续用）
    if x_device_id is None or x_device_id != payload.device_id:
        await revoke_token(db, token)
        await commit_with_redis(db, redis)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "device_changed")

    # 每日首次续期（parent 生效；子 token expires_at=None 时 needs_roll 永远 False）
    if needs_roll(payload):
        payload = await roll_token_expiry(
            db, token_hash_hex=token_hash(token), payload=payload,
        )
        await commit_with_redis(db, redis)

    return CurrentAccount(
        id=payload.user_id,
        role=payload.role,
        family_id=payload.family_id,
        expires_at=payload.expires_at,
    )


async def require_parent(
    current: Annotated[CurrentAccount, Depends(get_current_account)],
) -> CurrentAccount:
    """断言 role==parent；否则 403。"""
    if current.role != UserRole.parent:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "parent required")
    return current


async def require_child(
    current: Annotated[CurrentAccount, Depends(get_current_account)],
) -> CurrentAccount:
    """断言 role==child；否则 403。"""
    if current.role != UserRole.child:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "child required")
    return current
