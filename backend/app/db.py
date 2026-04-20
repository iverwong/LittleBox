"""FastAPI async SQLAlchemy session 工厂。业务 handler 走 commit_with_redis,
不要在 yield 后显式 commit/close。CLI 不复用本模块的 _engine,走 _common.cli_runtime()。"""
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

_engine = create_async_engine(settings.database_url, pool_pre_ping=True)
_session_maker = async_sessionmaker(_engine, expire_on_commit=False)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with _session_maker() as session:
        yield session

async def dispose_engine() -> None:
    """挂到 main.py lifespan shutdown;Step 5 Redis lifespan 落地时一并挂。"""
    await _engine.dispose()
