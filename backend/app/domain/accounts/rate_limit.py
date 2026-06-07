"""父端登录限流(M5 / M6)。

Redis 计数器 + 60s 滑动窗口,phone 与 IP 双维度独立计数。

IP 维度降级: 当 ip=None (解析不到可信客户端 IP) 时, 跳过 IP 桶检查 / 递增,
避免把所有"未知 IP"请求合并到同一个共享桶而触发误伤式 DoS。
phone 桶始终参与, 保留单账号爆破的硬上限。

边界: 限流逻辑属 accounts 域, 与 auth 协议(API 路由)解耦。模块公开 API
使用无下划线前缀命名(check_login_limit / incr_login_fail),与原
app/api/auth.py 的私有 helper 区别。
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, status
from redis.asyncio import Redis

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量集中防漂移
# ---------------------------------------------------------------------------

LOGIN_PHONE_LIMIT = 5
LOGIN_IP_LIMIT = 20
LOGIN_WINDOW_SECONDS = 60


# ---------------------------------------------------------------------------
# 限流 helper (从 app/api/auth.py 抽离)
# ---------------------------------------------------------------------------


async def check_login_limit(redis: Redis, phone: str, ip: str | None) -> None:
    """检查是否已达限流阈值,是则 raise 429。

    IP 维度降级: 当 ip=None (解析不到可信客户端 IP) 时, 跳过 IP 桶检查,
    避免把所有"未知 IP"请求合并到同一个共享桶而触发误伤式 DoS。
    phone 桶始终参与, 保留单账号爆破的硬上限。
    """
    phone_count = int(await redis.get(f"login_fail:phone:{phone}") or 0)
    if phone_count >= LOGIN_PHONE_LIMIT:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts; try again later")
    if ip is not None:
        ip_count = int(await redis.get(f"login_fail:ip:{ip}") or 0)
        if ip_count >= LOGIN_IP_LIMIT:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts; try again later"
            )


async def incr_login_fail(redis: Redis, phone: str, ip: str | None) -> None:
    """失败一次,递增 phone 桶 (ip=None 时跳过 IP 桶)。

    同样地, IP 不可信时不递增 IP 计数 —— 不让 "unknown" 共享桶被建立。

    Redis 写入策略: 失败路径无 DB 状态变更, 走 redis.pipeline 直写,
    不经 stage_redis_op/commit_with_redis (避免一次多余的 commit 触发)。
    成功路径 (login) 走 staging, 随 commit_with_redis 一起 flush,
    保证 DB token 签发 + Redis 计数清零的原子性。
    """
    async with redis.pipeline(transaction=False) as pipe:
        pipe.incr(f"login_fail:phone:{phone}")
        pipe.expire(f"login_fail:phone:{phone}", LOGIN_WINDOW_SECONDS, nx=True)
        if ip is not None:
            pipe.incr(f"login_fail:ip:{ip}")
            pipe.expire(f"login_fail:ip:{ip}", LOGIN_WINDOW_SECONDS, nx=True)
        await pipe.execute()
