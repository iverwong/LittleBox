"""Redis 连接工厂 + FastAPI Depends; Step 5 补充 lifespan。"""
from redis.asyncio import Redis

_redis: Redis | None = None

async def get_redis() -> Redis:
    assert _redis is not None, "redis pool not initialized (check lifespan)"
    return _redis
