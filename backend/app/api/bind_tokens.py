"""bind_tokens 路由：绑定凭证完整生命周期（创建 / status / redeem）。"""
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
    peek_bind_token,
    stage_consume_bind_token,
    stage_record_bind_result,
)
from app.auth.deps import require_parent
from app.auth.redis_client import get_redis
from app.auth.redis_ops import commit_with_redis
from app.auth.tokens import issue_token, revoke_all_active_tokens
from app.db import get_db
from app.models.accounts import User
from app.models.enums import UserRole
from app.schemas.accounts import (
    AccountOut,
    BindTokenResponse,
    BindTokenStatusOut,
    CreateBindTokenRequest,
    LoginResponse,
    RedeemBindTokenRequest,
)

router = APIRouter(prefix="/api/v1/bind-tokens", tags=["bind_tokens"])


@router.post("", response_model=BindTokenResponse, status_code=201)
async def create_bind_token(
    payload: CreateBindTokenRequest,
    parent: Annotated[User, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> BindTokenResponse:
    """为指定 child 生成一次性绑定 token（parent 调用）。

    家庭边界：child 必须属于当前 parent 所在的 family。
    """
    stmt = select(User).where(
        User.id == payload.child_user_id,
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


@router.get("/{bind_token}/status", response_model=BindTokenStatusOut)
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


@router.post("/{bind_token}/redeem", response_model=LoginResponse)
async def redeem_bind_token(
    bind_token: str,
    payload: RedeemBindTokenRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> LoginResponse:
    """子端扫码后换取永久 child token；同时吊销该 child 所有老 token。

    bind_token 从 path param 取；body 仅携带 device_id（device_info B0 不再由客户端传入）。
    """
    # peek 不删；DB 写入成功后 stage_consume_bind_token 入 staging，
    # 由 commit_with_redis 统一 flush（DB 回滚则 bind_token 保留，5min TTL 内可重试）
    peeked = await peek_bind_token(redis, bind_token)
    if peeked is None:
        raise HTTPException(400, "bind token invalid or expired")
    _parent_id, child_id = peeked
    child = await db.get(User, child_id)
    if child is None or not child.is_active or child.role != UserRole.child:
        raise HTTPException(400, "child account unavailable")

    # 新设备扫码吊销该 child 所有活跃 token（与 /auth/login 对齐）
    await revoke_all_active_tokens(db, child.id)
    token = await issue_token(
        db,
        user_id=child.id,
        role=child.role,
        family_id=child.family_id,
        ttl_days=None,  # 永不过期
        device_id=payload.device_id,
        device_info=None,  # B0: device_info no longer passed by client
    )
    stage_consume_bind_token(db, bind_token)
    # 同时 stage 一条 bind_result 供父端轮询端点读；DB 回滚则两条同时丢，父端保持看到 pending
    stage_record_bind_result(db, bind_token, child.id)
    await commit_with_redis(db, redis)
    return LoginResponse(
        token=token,
        account=AccountOut(
            id=child.id,
            role=child.role,
            family_id=child.family_id,
            phone=None,
            is_active=True,
        ),
    )
