"""父端登录限流。

基于 Redis 主 db=0 计数器,phone 与 IP 双桶独立计数,共享 60s 滑动窗口。

IP 桶降级:当 `ip=None`(解析不到可信客户端 IP)时,跳过 IP 桶检查与递增,
避免所有"未知 IP"请求合并到同一个共享桶而触发误伤式 DoS;phone 桶始终参与,
保留单账号爆破的硬上限。

边界:限流逻辑属 accounts 域,与 auth 协议(API 路由)解耦。模块公开
API 使用无下划线前缀命名(`check_login_limit` / `incr_login_fail`),与
`app/api/auth.py` 的私有 helper 区分。
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, status
from redis.asyncio import Redis

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量集中防漂移
# ---------------------------------------------------------------------------

# phone 桶单账号硬上限
LOGIN_PHONE_LIMIT = 5
# ip 桶单 IP 宽限上限(防同一出口下多账号误伤)
LOGIN_IP_LIMIT = 20
# 计数窗口长度,expire(nx=True) 设到 key 上,自然过期清零
LOGIN_WINDOW_SECONDS = 60

# Redis key 命名空间:phone 与 ip 双桶独立,前缀与 token / session 等域隔开
LOGIN_FAIL_PHONE_KEY_PREFIX = "login_fail:phone:"
LOGIN_FAIL_IP_KEY_PREFIX = "login_fail:ip:"


# ---------------------------------------------------------------------------
# 限流 helper
# ---------------------------------------------------------------------------


async def check_login_limit(redis: Redis, phone: str, ip: str | None) -> None:
    """检查是否已达登录限流阈值,达到则抛 429。

    先查 phone 桶(必查),如已达 `LOGIN_PHONE_LIMIT` 直接抛错;再查 ip 桶
    (仅当 `ip` 可信时),如已达 `LOGIN_IP_LIMIT` 抛错。`ip=None` 时跳过
    IP 桶检查,避免"未知 IP"共享桶被建立后误伤所有无法解析 IP 的请求。

    Args:
        redis: Redis 客户端。
        phone: 登录手机号,作为 phone 桶 key 后缀。
        ip: 客户端 IP(由 uvicorn 净化后提供),`None` 表示跳过 IP 维度。

    Raises:
        HTTPException: 429,达到 phone 或 ip 桶上限。
    """
    phone_count = int(await redis.get(f"{LOGIN_FAIL_PHONE_KEY_PREFIX}{phone}") or 0)
    if phone_count >= LOGIN_PHONE_LIMIT:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts; try again later")
    if ip is not None:
        ip_count = int(await redis.get(f"{LOGIN_FAIL_IP_KEY_PREFIX}{ip}") or 0)
        if ip_count >= LOGIN_IP_LIMIT:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts; try again later"
            )


async def incr_login_fail(redis: Redis, phone: str, ip: str | None) -> None:
    """登录失败一次,递增对应计数桶。

    `ip` 不可信时跳过 IP 桶递增,不建立"unknown"共享桶。phone 桶始终递增。
    INCR + EXPIRE(NX) 合并到一条 pipeline 执行,失败路径无 DB 状态变更,
    走 `redis.pipeline` 直写,不经 `stage_redis_op`/`commit_with_redis`,
    避免失败路径触发多余的 DB commit。成功路径由 `app/api/auth.py`
    走 staging,随 `commit_with_redis` 一起 flush,保证 DB token 签发与
    Redis 计数清零的原子性。

    Args:
        redis: Redis 客户端。
        phone: 登录手机号,作为 phone 桶 key 后缀。
        ip: 客户端 IP,`None` 时跳过 IP 桶。
    """
    async with redis.pipeline(transaction=False) as pipe:
        pipe.incr(f"{LOGIN_FAIL_PHONE_KEY_PREFIX}{phone}")
        pipe.expire(f"{LOGIN_FAIL_PHONE_KEY_PREFIX}{phone}", LOGIN_WINDOW_SECONDS, nx=True)
        if ip is not None:
            pipe.incr(f"{LOGIN_FAIL_IP_KEY_PREFIX}{ip}")
            pipe.expire(f"{LOGIN_FAIL_IP_KEY_PREFIX}{ip}", LOGIN_WINDOW_SECONDS, nx=True)
        await pipe.execute()
