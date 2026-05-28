"""进程级资源容器。FastAPI lifespan / ARQ on_startup 共用。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from langgraph.graph.state import CompiledStateGraph
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.auth.redis_client import _build_arq_redis_url

if TYPE_CHECKING:
    from arq.connections import ArqRedis

    from app.config import Settings


@dataclass(frozen=True)
class RuntimeResources:
    """进程级资源容器，构建后不可变。"""
    settings: Settings
    db_engine: AsyncEngine
    db_session_factory: async_sessionmaker[AsyncSession]
    audit_redis: Redis
    arq_pool: ArqRedis
    main_graph: CompiledStateGraph
    audit_graph: CompiledStateGraph


async def build_runtime(settings: Settings) -> RuntimeResources:
    """构建进程级资源容器；FastAPI lifespan / ARQ on_startup 共用。"""
    # 1. db_engine（参数对齐 app/db.py::_engine）
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)

    # 2. session factory
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # 3. audit_redis（派生自 settings.redis_url，db=arq_redis_db，_build_arq_redis_url 单一来源）
    audit_redis_url = _build_arq_redis_url()
    parsed = urlparse(settings.redis_url)  # 单独解析供 arq_pool host/port/password
    audit_redis = Redis.from_url(audit_redis_url, encoding="utf-8", decode_responses=True)

    # 4. arq_pool（构造参数对齐 worker.py::RedisSettings）
    from arq import create_pool
    from arq.connections import RedisSettings as ArqRedisSettings
    arq_pool = await create_pool(
        ArqRedisSettings(
            host=parsed.hostname or "localhost",
            port=parsed.port or 6379,
            password=parsed.password,
            database=settings.arq_redis_db,
        ),
    )

    # 5. main_graph（惰性导入：当前 commit 时 build_main_graph 尚不存在）
    from app.chat.graph import build_main_graph
    main_graph = build_main_graph()

    # 6. audit_graph（惰性导入；T11 改无参工厂 + Runtime DI）
    from app.audit.graph import build_audit_graph
    audit_graph = build_audit_graph()

    return RuntimeResources(
        settings=settings,
        db_engine=engine,
        db_session_factory=session_factory,
        audit_redis=audit_redis,
        arq_pool=arq_pool,
        main_graph=main_graph,
        audit_graph=audit_graph,
    )


async def teardown_runtime(rr: RuntimeResources) -> None:
    """反向关闭：arq_pool → audit_redis → db_engine。图无需关闭。"""
    await rr.arq_pool.close(close_connection_pool=True)
    await rr.audit_redis.aclose()
    await rr.db_engine.dispose()
