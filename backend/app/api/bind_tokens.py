"""子端绑定凭证路由:``/api/v1/bind-tokens``。

承载一次性 ``bind_token`` 的完整生命周期:父端创建、子端兑换、父端轮询状态。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.enums import UserRole
from app.core.redis import commit_with_redis, get_redis
from app.domain.accounts.models import User
from app.domain.accounts.schemas import AccountOut, CurrentAccount
from app.domain.auth.bind_tokens import (
    BIND_KEY_PREFIX,
    BIND_RESULT_KEY_PREFIX,
    consume_bind_token,
    issue_bind_token,
    stage_record_bind_result,
)
from app.domain.auth.deps import require_parent
from app.domain.auth.schemas import (
    BindTokenResponse,
    BindTokenStatusOut,
    CreateBindTokenRequest,
    LoginResponse,
    RedeemBindTokenRequest,
)
from app.domain.auth.tokens import issue_token, revoke_all_active_tokens

router = APIRouter(prefix="/api/v1/bind-tokens", tags=["bind_tokens"])


@router.post("", response_model=BindTokenResponse, status_code=201)
async def create_bind_token(
    payload: CreateBindTokenRequest,
    parent: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> BindTokenResponse:
    """为指定 child 生成一次性绑定 token(``POST /api/v1/bind-tokens``)。

    父端调用:校验 child 属于当前 parent 所在的 family,通过后
    ``issue_bind_token`` 写入 5 分钟 TTL 的 Redis key。

    Args:
        payload: ``CreateBindTokenRequest``,含 ``child_user_id``。
        parent: 当前父账号上下文(``Depends(require_parent)``)。
        db: 异步 SQLAlchemy session(``Depends(get_db)``)。
        redis: 主业务 Redis(``Depends(get_redis)``)。

    Returns:
        ``BindTokenResponse``:含明文 ``bind_token``。

    Raises:
        HTTPException ``status.HTTP_404_NOT_FOUND``:目标 child 不存在、
            非本 family、或不是 child 角色(不区分,统一 404 避免泄露存在性)。
    """
    stmt = select(User).where(
        User.id == payload.child_user_id,
        User.role == UserRole.child,
        User.family_id == parent.family_id,
        User.is_active.is_(True),
    )
    child = (await db.execute(stmt)).scalar_one_or_none()
    if child is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "child not found in family")

    token = await issue_bind_token(
        redis,
        parent_user_id=parent.id,
        child_user_id=child.id,
    )
    return BindTokenResponse(bind_token=token)


@router.get("/{bind_token}/status", response_model=BindTokenStatusOut)
async def get_bind_token_status(
    bind_token: str,
    redis: Annotated[Redis, Depends(get_redis)],
) -> BindTokenStatusOut:
    """轮询 ``bind_token`` 兑换状态,``GET /api/v1/bind-tokens/{bind_token}/status``。

    优先级:

    1. ``bind_result:{token}`` 存在 → ``status="bound"``,带 ``child_user_id`` / ``bound_at``
    2. ``bind:{token}`` 还活着 → ``status="pending"``
    3. 两者皆无 → ``404``

    **不鉴权**:``bind_token`` 本身是一次性机密凭证(5min TTL + 16 字节
    urlsafe 随机),持有即视为父端;与 ``create_bind_token`` 端点的
    ``require_parent`` 对称闭合。

    Args:
        bind_token: 待查询的 bind_token 字符串(path param)。
        redis: 主业务 Redis(``Depends(get_redis)``)。

    Returns:
        ``BindTokenStatusOut``:状态码与绑定结果。

    Raises:
        HTTPException ``status.HTTP_404_NOT_FOUND``:
            ``bind_token`` 已过期且未兑换,或根本不存在。
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
    raise HTTPException(status.HTTP_404_NOT_FOUND, "bind token not found or expired")


@router.post("/{bind_token}/redeem", response_model=LoginResponse)
async def redeem_bind_token(
    bind_token: str,
    payload: RedeemBindTokenRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> LoginResponse:
    """子端扫码后兑换永久 child token,``POST /api/v1/bind-tokens/{bind_token}/redeem``。

    同时吊销该 child 的全部老 token(对齐 ``/auth/login`` 的"一次一设备"语义),
    并 stage 一条 ``bind_result`` 供父端轮询端点读取。

    ``bind_token`` 从 path param 取;body 仅携带 ``device_id``(客户端不再
    传入 ``device_info``)。

    ``consume_bind_token`` 用 Redis ``GETDEL`` 原子地"读+删",并发
    redeem 第二个必拿 ``None``,杜绝双发 token。``bind_token`` 拿到后
    无论 DB 写入成败都不可重试(故意为之:若 DB 无 ``auth_token`` 记录,
    客户端即使拿到 ``bind_token`` 也不该再有重试机会)。

    Args:
        bind_token: 一次性绑定凭证(path param)。
        payload: ``RedeemBindTokenRequest``,含 ``device_id``。
        db: 异步 SQLAlchemy session(``Depends(get_db)``)。
        redis: 主业务 Redis(``Depends(get_redis)``)。

    Returns:
        ``LoginResponse``:含明文 child token(永不过期)与 ``AccountOut``。

    Raises:
        HTTPException ``status.HTTP_400_BAD_REQUEST``:
            ``bind_token`` 无效 / 已过期 / 已被兑换;或目标 child 账号
            不可用(被禁用、角色不是 child)。
    """
    peeked = await consume_bind_token(redis, bind_token)
    if peeked is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bind token invalid or expired")
    _parent_id_at_issue, child_id = peeked
    # 注:此处未对 _parent_id_at_issue 做 family 边界校验。如需防御运维 SQL
    # 误改 family_id,可在 child 查询通过后补判
    # (parent_at_issue.family_id == child.family_id + parent is_active + role=parent)。
    child = await db.get(User, child_id)
    if child is None or not child.is_active or child.role != UserRole.child:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "child account unavailable")

    # 新设备扫码吊销该 child 所有活跃 token（与 /auth/login 对齐）
    await revoke_all_active_tokens(db, child.id)
    token = await issue_token(
        db,
        user_id=child.id,
        role=child.role,
        family_id=child.family_id,
        ttl_days=None,  # 永不过期
        device_id=payload.device_id,
        device_info=None,  # 客户端不再传入 device_info
    )
    # stage 一条 bind_result 供父端轮询端点读;DB 回滚则此条也不 flush,父端保持看到 pending
    stage_record_bind_result(db, bind_token, child.id)
    await commit_with_redis(db, redis)
    return LoginResponse(
        token=token,
        account=AccountOut(
            id=child.id,
            role=child.role,
            family_id=child.family_id,
            phone=child.phone,
            is_active=child.is_active,
        ),
    )
