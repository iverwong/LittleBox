"""父端登录与登出路由：``POST /api/v1/auth/login`` 与 ``POST /api/v1/auth/logout``。

登录处理 phone + password 鉴权、Redis 限流与 token 签发；
登出吊销当前父账号的 token。"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.enums import UserRole
from app.core.redis import RedisOp, commit_with_redis, get_redis, stage_redis_op
from app.domain.accounts.models import User
from app.domain.accounts.rate_limit import (
    LOGIN_FAIL_IP_KEY_PREFIX,
    LOGIN_FAIL_PHONE_KEY_PREFIX,
    check_login_limit,
    incr_login_fail,
)
from app.domain.accounts.schemas import AccountOut, CurrentAccount
from app.domain.auth.deps import require_parent
from app.domain.auth.password import verify_password
from app.domain.auth.schemas import LoginRequest, LoginResponse
from app.domain.auth.tokens import (
    issue_token,
    revoke_all_active_tokens,
    revoke_token,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _get_client_ip(request: Request) -> str | None:
    """返回 uvicorn 净化后的客户端 IP,无 client 信息时返回 ``None``。

    合同：不解析 ``X-Forwarded-For`` / ``X-Real-IP`` / 任何代理头。

    反代部署应让 uvicorn 的 ``ProxyHeadersMiddleware`` 完成 IP 净化:

    .. code-block:: bash

        uvicorn app.main:app --proxy-headers \\
            --forwarded-allow-ips=<反代 IP 或 CIDR>

    uvicorn 据此:
      1. 校验直接 peer IP 是否在 ``--forwarded-allow-ips`` 白名单
      2. 是 → 取 ``X-Forwarded-For`` 最右一个非可信跳,写入 ``scope["client"].host``
      3. 否 → 忽略 ``X-Forwarded-For``,``scope["client"]`` 保留真实 peer IP

    之后本函数返回的就是 uvicorn 净化后的客户端 IP,业务代码无
    任何额外信任判断,也不再有可被伪造的接缝。

    返回 ``None`` 表示 ASGI scope 未传 ``client``(常见于裸 socket
    部署或 ASGI 异常)。调用方(如限流)应将 ``None`` 视为"不参与该维度
    限流",而非塞进 ``"unknown"`` 共享桶。

    Args:
        request: 当前 FastAPI 请求对象。

    Returns:
        uvicorn 净化后的客户端 IP,或 ``None``。
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
    """父账号登录:phone + password → opaque token。

    ``POST /api/v1/auth/login``。流程:

    1. 取客户端 IP(``None`` 时跳过 IP 维度限流)
    2. ``check_login_limit``:phone / IP 双桶已达阈值则 ``429``
    3. 查 ``User``(role=parent, is_active),账号不存在或密码错统一 ``401``
    4. 失败路径 ``incr_login_fail`` 递增计数;成功路径 stage 清零两条 key
    5. ``revoke_all_active_tokens`` 吊销该 parent 全部老 token(一次一设备)
    6. ``issue_token`` 签 7 天有效的新 token,落 DB + stage Redis
    7. ``commit_with_redis`` 原子落盘

    Args:
        request: FastAPI 请求对象,用于读取 ``client.host``。
        payload: ``LoginRequest``(phone / password / device_id)。
        db: 异步 SQLAlchemy session(``Depends(get_db)``)。
        redis: 主业务 Redis(``Depends(get_redis)``)。

    Returns:
        ``LoginResponse``:含明文 token 与 ``AccountOut``。

    Raises:
        HTTPException ``status.HTTP_429_TOO_MANY_REQUESTS``:登录限流触发。
        HTTPException ``status.HTTP_401_UNAUTHORIZED``:账号不存在、密码错
            或账号无 password_hash。
    """
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
        # user 为 None 时 verify_password 无可验证对象;password_hash 为
        # None 属于数据不一致,按"密码错"路径处理以避免泄露账号存在性。
        await incr_login_fail(redis, payload.phone, client_ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if not verify_password(user.password_hash, payload.password):
        await incr_login_fail(redis, payload.phone, client_ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    # 成功：清零两个计数器 (走 staging, 随 commit_with_redis 一起 flush)
    stage_redis_op(db, RedisOp(kind="delete", key=f"{LOGIN_FAIL_PHONE_KEY_PREFIX}{payload.phone}"))
    if client_ip is not None:
        stage_redis_op(db, RedisOp(kind="delete", key=f"{LOGIN_FAIL_IP_KEY_PREFIX}{client_ip}"))

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
    """主动下线当前父账号 token,``POST /api/v1/auth/logout``。仅 parent。

    token 从 ``request.state.token`` 取(``get_current_account`` 已 stash),
    不再二次 split Authorization header,避免把 ``"Authorization: Bearer
    / Token xxx"`` 之类的畸形头静默吞成 no-op。

    Args:
        request: FastAPI 请求对象,用于读取 ``request.state.token``。
        current: 当前账号上下文(``Depends(require_parent)``)。
        db: 异步 SQLAlchemy session(``Depends(get_db)``)。
        redis: 主业务 Redis(``Depends(get_redis)``)。

    Returns:
        无。

    Raises:
        HTTPException ``status.HTTP_401_UNAUTHORIZED``:缺失 / 非法 Bearer
            token(``get_current_account`` 抛出)。
        HTTPException ``status.HTTP_403_FORBIDDEN``:非 parent 角色
            (``require_parent`` 抛出)。
    """
    await revoke_token(db, request.state.token)
    await commit_with_redis(db, redis)
