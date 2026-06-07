"""auth 路由：login / logout。"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_parent
from app.auth.password import verify_password
from app.auth.tokens import (
    issue_token,
    revoke_all_active_tokens,
    revoke_token,
)
from app.core.db import get_db
from app.core.redis import RedisOp, commit_with_redis, get_redis, stage_redis_op
from app.domain.accounts.rate_limit import (
    check_login_limit,
    incr_login_fail,
)
from app.domain.accounts.schemas import AccountOut, CurrentAccount
from app.domain.auth.schemas import LoginRequest, LoginResponse
from app.models.accounts import User
from app.models.enums import UserRole

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _get_client_ip(request: Request) -> str | None:
    """返回 uvicorn 净化后的客户端 IP, 无 client 信息时返回 None。

    合同: 不解析 XFF / X-Real-IP / 任何代理头。

    反代部署应让 uvicorn 的 ProxyHeadersMiddleware 完成 IP 净化:
        uvicorn app.main:app --proxy-headers \\
            --forwarded-allow-ips=<反代 IP 或 CIDR>
    uvicorn 据此:
        1. 校验直接 peer IP 是否在 --forwarded-allow-ips 白名单
        2. 是 → 取 XFF 最右一个非可信跳, 写入 scope["client"].host
        3. 否 → 忽略 XFF, scope["client"] 保留真实 peer IP

    之后本函数返回的就是 uvicorn 净化后的客户端 IP, 业务代码无
    任何额外信任判断, 也不再有可被伪造的接缝。

    None 语义: 返回 None 表示 ASGI scope 未传 client (常见于裸 socket
    部署或 ASGI 异常)。调用方 (如限流) 应将 None 视为"不参与该维度
    限流", 而非塞进 "unknown" 共享桶。

    历史: 早期实现曾在 app 层做 XFF 最左段解析, 已删除。
    trust_proxy_headers / LB_TRUST_PROXY_HEADERS 已同步移除, 不再使用。
    """
    if request.client and request.client.host:
        return request.client.host
    return None


@router.post("/login", response_model=LoginResponse)
async def login(
    request: Request,
    payload: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> LoginResponse:
    """父账号登录：phone + password → opaque token。"""
    client_ip = _get_client_ip(request)
    if client_ip is None:
        # 解析不到 peer IP —— 裸 socket 部署 + scope.client 缺失
        # 或 ASGI 异常。这里只 WARN 不阻断 (fail-open): phone 桶仍在,
        # 单账号爆破被卡住。生产 uvicorn 直连下此日志不应出现。
        logger.warning(
            "login without resolvable client IP path=%s ua=%r",
            request.url.path,
            request.headers.get("user-agent"),
        )
    await check_login_limit(redis, payload.phone, client_ip)

    # 统一 401，不区分账号不存在 / 密码错（防枚举）
    stmt = select(User).where(
        User.phone == payload.phone,
        User.role == UserRole.parent,
        User.is_active.is_(True),
    )
    user = (await db.execute(stmt)).scalar_one_or_none()
    if user is None or user.password_hash is None:
        await incr_login_fail(redis, payload.phone, client_ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if not verify_password(user.password_hash, payload.password):
        await incr_login_fail(redis, payload.phone, client_ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    # 成功：清零两个计数器 (走 staging, 随 commit_with_redis 一起 flush)
    stage_redis_op(db, RedisOp(kind="delete", key=f"login_fail:phone:{payload.phone}"))
    if client_ip is not None:
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


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    current: Annotated[CurrentAccount, Depends(require_parent)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> None:
    """主动下线当前父账号 token。限 parent。

    token 从 `request.state.token` 取 (get_current_account 已 stash),
    不再二次 split Authorization header —— 避免前次审查发现的
    "Authorization: Bearer / Token xxx" 静默 no-op 漏洞。
    """
    await revoke_token(db, request.state.token)
    await commit_with_redis(db, redis)
