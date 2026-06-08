"""进程级资源容器。FastAPI lifespan / ARQ on_startup 共用。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
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

from app.core.redis import _build_arq_redis_url

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from arq.connections import ArqRedis

    from app.core.config import Settings


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
    # M9-patch1: 活跃 chat bg task 登记表，供 lifespan shutdown 等候 + cancel
    _chat_tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    def register_chat_task(self, sid: str, task: asyncio.Task) -> None:
        """登记一条 chat bg task，自动在完成时从登记表移除。

        通过 add_done_callback 实现自动清理：
        1. task 完成后自动从 _chat_tasks pop
        2. 若 task 抛出未捕获异常，日志留痕（含 sid 上下文）

        异常注意：
        - t.exception() 必须在 if not t.cancelled() 守卫后调用，
          否则已取消的 task 调用 exception() 会抛 CancelledError
        - exception() 调用同时「消费」异常，避免 "Task exception was
          never retrieved" 告警
        """
        self._chat_tasks[sid] = task

        def _on_done(t: asyncio.Task) -> None:
            # 身份守卫：仅 pop 当前注册的 task。
            # sid 唯一性由外部保障（acquire_session_lock 409 SessionBusy +
            # running_streams 注册早于 create_task），此守卫为防御性编程。
            if self._chat_tasks.get(sid) is not t:
                return
            self._chat_tasks.pop(sid, None)
            if not t.cancelled():
                if exc := t.exception():
                    logger.error(
                        "chat task crashed unhandled",
                        extra={"sid": sid},
                        exc_info=exc,
                    )

        task.add_done_callback(_on_done)


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
    from app.domain.chat.graph import build_main_graph

    main_graph = build_main_graph()

    # 6. audit_graph（惰性导入；T11 改无参工厂 + Runtime DI）
    from app.domain.audit.graph import build_audit_graph

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
    await rr.arq_pool.aclose(close_connection_pool=True)
    await rr.audit_redis.aclose()
    await rr.db_engine.dispose()
