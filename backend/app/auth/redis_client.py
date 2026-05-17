"""Redis 连接工厂 + FastAPI Depends + lifespan。"""
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import urlparse, urlunparse

from redis.asyncio import Redis

from app.config import settings

_redis: Redis | None = None
_audit_redis: Redis | None = None


def _build_arq_redis_url() -> str:
    """从 settings.redis_url 派生 ARQ 专用 db URL（db=arq_redis_db）。"""
    parsed = urlparse(settings.redis_url)
    return urlunparse(parsed._replace(path=f"/{settings.arq_redis_db}"))


@asynccontextmanager
async def redis_lifespan() -> AsyncIterator[None]:
    """挂到 FastAPI lifespan：创建主 Redis + ARQ Redis 连接池。"""
    global _redis, _audit_redis
    _redis = Redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )
    _audit_redis = Redis.from_url(
        _build_arq_redis_url(),
        encoding="utf-8",
        decode_responses=True,
    )
    try:
        yield
    finally:
        await _redis.aclose()
        await _audit_redis.aclose()
        _redis = _audit_redis = None


async def get_redis() -> Redis:
    """主业务 Redis（db=0）。"""
    assert _redis is not None, "redis pool not initialized (check lifespan)"
    return _redis


async def get_audit_redis() -> Redis:
    """ARQ 队列 + 审查信号管道 Redis（db=arq_redis_db，通常=1）。"""
    assert _audit_redis is not None, (
        "audit redis pool not initialized (check lifespan)"
    )
    return _audit_redis
