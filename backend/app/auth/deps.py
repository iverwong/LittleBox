"""FastAPI Depends：get_current_account / require_parent / require_child."""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.redis_client import get_redis
from app.auth.tokens import resolve_token
from app.db import get_db  # type: ignore[attr-defined]
from app.models.enums import UserRole
from app.schemas.accounts import CurrentAccount


async def get_current_account(
    authorization: Annotated[str | None, Header()] = None,
    x_device_id: Annotated[str | None, Header(alias="X-Device-Id")] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = ...,  # type: ignore[assignment]
    redis: Annotated[Redis, Depends(get_redis)] = ...,  # type: ignore[assignment]
) -> CurrentAccount:
    """从 Authorization: Bearer 解析 token 并返回当前账号。"""
    raise NotImplementedError


async def require_parent(
    current: Annotated[CurrentAccount, Depends(get_current_account)],
) -> CurrentAccount:
    """断言 role==parent；否则 403。"""
    raise NotImplementedError


async def require_child(
    current: Annotated[CurrentAccount, Depends(get_current_account)],
) -> CurrentAccount:
    """断言 role==child；否则 403。"""
    raise NotImplementedError
