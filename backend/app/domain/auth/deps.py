"""FastAPI Depends 集:鉴权中间件依赖(`get_current_account` / `require_parent` / `require_child`)。

被 `api/auth.py` / `api/me.py` / `api/bind_tokens.py` / `api/children.py` 大量消费,
统一通过 Bearer token + X-Device-Id 头解析当前账号并按 role 守门。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db  # type: ignore[attr-defined]
from app.core.enums import UserRole
from app.core.redis import commit_with_redis, get_redis
from app.domain.accounts.schemas import CurrentAccount
from app.domain.auth.tokens import (
    needs_roll,
    resolve_token,
    revoke_token,
    roll_token_expiry,
    token_hash,
)


async def get_current_account(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_device_id: Annotated[str | None, Header(alias="X-Device-Id")] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = ...,  # type: ignore[assignment]
    redis: Annotated[Redis, Depends(get_redis)] = ...,  # type: ignore[assignment]
) -> CurrentAccount:
    """从 Authorization Bearer 头解析 token 并返回当前账号上下文。

    流程:
    1. 校验 Bearer 前缀并切出明文 token,缺失则 401。
    2. `resolve_token` 先查 Redis,miss 再查 DB 并回填 Redis;无效或过期返回 None → 401。
    3. 比对 X-Device-Id 头与 payload.device_id,不匹配则 `revoke_token` + 401(fail-closed,
       防止 token 泄漏后换设备继续使用)。
    4. parent token 每日首次命中时 `needs_roll` 为真,触发 `roll_token_expiry` 续期;
       子端 `expires_at=None` 时该判断恒为 False。

    副作用:把解析出的明文 token 写入 `request.state.token`,供同请求链上的 handler
    (如 `/auth/logout`)直接 `revoke_token`,避免二次 split Authorization header。

    Args:
        request: FastAPI Request;用于读取/写入 `state.token`。
        authorization: Authorization 头原始值;期望 `Bearer <token>` 形式。
        x_device_id: X-Device-Id 头;与 token 绑定的设备 UUID 比对。
        db: 通过 Depends 注入的 async DB session(由 handler 关闭/commit)。
        redis: 通过 Depends 注入的 async Redis 客户端。

    Returns:
        CurrentAccount: 当前账号的轻量上下文(user_id / role / family_id / expires_at)。

    Raises:
        HTTPException: status.HTTP_401_UNAUTHORIZED —
            `missing bearer token`(无 Bearer 前缀)/
            `invalid or expired token`(resolve_token 返回 None)/
            `device_changed`(X-Device-Id 与 payload.device_id 不一致)。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    request.state.token = token
    payload = await resolve_token(db, redis, token)
    if payload is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token")

    # 设备绑定:比对 X-Device-Id 头与 auth_tokens.device_id。
    # 不匹配 → 立即吊销 token + 401(fail-closed,防止 token 泄漏后换设备继续使用)。
    if x_device_id is None or x_device_id != payload.device_id:
        await revoke_token(db, token)
        await commit_with_redis(db, redis)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "device_changed")

    # 每日首次续期(parent 生效;子 token `expires_at=None` 时 needs_roll 恒为 False)。
    if needs_roll(payload):
        payload = await roll_token_expiry(
            db,
            token_hash_hex=token_hash(token),
            payload=payload,
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
    """断言当前账号角色为 parent,否则 403。

    Args:
        current: 由 `get_current_account` 注入的当前账号上下文。

    Returns:
        CurrentAccount: 透传 `current`,角色限定为 parent。

    Raises:
        HTTPException: status.HTTP_403_FORBIDDEN — 角色非 parent 时抛出 `parent required`。
    """
    if current.role != UserRole.parent:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "parent required")
    return current


async def require_child(
    current: Annotated[CurrentAccount, Depends(get_current_account)],
) -> CurrentAccount:
    """断言当前账号角色为 child,否则 403。

    Args:
        current: 由 `get_current_account` 注入的当前账号上下文。

    Returns:
        CurrentAccount: 透传 `current`,角色限定为 child。

    Raises:
        HTTPException: status.HTTP_403_FORBIDDEN — 角色非 child 时抛出 `child required`。
    """
    if current.role != UserRole.child:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "child required")
    return current
