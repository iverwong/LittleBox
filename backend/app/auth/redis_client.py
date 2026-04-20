"""Redis 连接工厂 + FastAPI Depends + lifespan。"""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from redis.asyncio import Redis

from app.config import settings

_redis: Redis | None = None


@asynccontextmanager
async def redis_lifespan() -> AsyncIterator[None]:
    """挂到 FastAPI lifespan：创建连接池 / 关闭。"""
    global _redis
    _redis = Redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )
    try:
        yield
    finally:
        await _redis.aclose()
        _redis = None


async def get_redis() -> Redis:
    """FastAPI Depends 工厂。"""
    assert _redis is not None, "redis pool not initialized (check lifespan)"
    return _redis
