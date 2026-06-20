"""进程级资源容器，FastAPI lifespan 与 ARQ on_startup 共用。

`build_runtime` / `teardown_runtime` 是单进程所有长生命周期资源
（DB engine / session 工厂 / 审查 Redis / ARQ 池 / 共享 httpx 客户端 /
编译后的 LangGraph）
的唯一构建与关闭路径，FastAPI、ARQ worker、CLI 脚本统一走它。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx
from langgraph.graph.state import CompiledStateGraph
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.llm_topology import LLM_HTTPX_TIMEOUT
from app.core.redis import _build_arq_redis_url

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from arq.connections import ArqRedis

    from app.core.config import Settings


@dataclass(frozen=True)
class RuntimeResources:
    """进程级资源容器，构建后不可变。

    由 `build_runtime` 创建；`teardown_runtime` 反向关闭。
    `frozen=True` 防止运行期被意外改写。

    Attributes:
        settings: 全局配置。
        db_engine: 异步 SQLAlchemy engine。
        db_session_factory: 异步 session 工厂（`get_db` 与后台任务共用）。
        audit_redis: 审查专用 Redis（db=`arq_redis_db`）。
        arq_pool: ARQ 异步任务队列连接池。
        shared_http_client: 进程级共享 `httpx.AsyncClient`，供 LLM transport
            复用 keep-alive 连接（避免每轮新建 httpx 池丢 TCP 握手）。
        main_graph: 主对话图（LangGraph 编译产物）。
        audit_graph: 审查图（LangGraph 编译产物）。
        _chat_tasks: 活跃 chat 后台任务登记表，
            供 lifespan shutdown 等候 + cancel。
    """

    settings: Settings
    db_engine: AsyncEngine
    db_session_factory: async_sessionmaker[AsyncSession]
    audit_redis: Redis
    arq_pool: ArqRedis
    shared_http_client: httpx.AsyncClient
    main_graph: CompiledStateGraph
    audit_graph: CompiledStateGraph
    _chat_tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    def register_chat_task(self, sid: str, task: asyncio.Task) -> None:
        """登记一条 chat 后台 task，并在 task 完成时自动从登记表移除。

        通过 `add_done_callback` 实现自动清理：task 完成后自动从
        `_chat_tasks` 弹出对应 sid；若 task 抛出未捕获异常则日志留痕
        （带 sid 上下文）。

        异常注意：
        - `t.exception()` 必须在 `if not t.cancelled()` 守卫后调用，
          否则已取消的 task 调用 `exception()` 会抛 `CancelledError`；
        - `exception()` 调用同时「消费」异常，避免
          "Task exception was never retrieved" 告警。

        Args:
            sid: session 标识，作为登记表 key。
            task: 已创建但未启动的 asyncio task。
        """
        self._chat_tasks[sid] = task

        def _on_done(t: asyncio.Task) -> None:
            # 身份守卫：仅 pop 当前注册的 task。
            # sid 唯一性由外部保障（`acquire_session_lock` 409 SessionBusy +
            # `running_streams` 注册早于 `create_task`），此守卫为防御性编程。
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
    """构建进程级资源容器。

    装配顺序：DB engine → session 工厂 → 审查 Redis → ARQ 池 →
    共享 httpx 客户端 → 主对话图 → 审查图。后两者用惰性 import，避免模块
    加载时即触发图编译。

    Args:
        settings: 全局配置。

    Returns:
        不可变的 `RuntimeResources` 实例。
    """
    # 1. db_engine
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)

    # 2. session 工厂
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # 3. audit_redis：派生自 `settings.redis_url`，`db=arq_redis_db`，
    #    `_build_arq_redis_url` 是单一来源。
    audit_redis_url = _build_arq_redis_url()
    parsed = urlparse(settings.redis_url)  # 单独解析供 arq_pool host/port/password 使用
    audit_redis = Redis.from_url(audit_redis_url, encoding="utf-8", decode_responses=True)

    # 4. arq_pool：构造参数对齐 `worker.py::RedisSettings`
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

    # 5. shared_http_client：进程级 httpx 连接池，供 LangChain 相关
    #    transport 复用 keep-alive。keepalive_expiry 30s 覆盖多数对话间隔；
    #    read 取 LLM_REQUEST_TIMEOUT_SECONDS（与 ChatDeepSeek(timeout=...) 同源）。
    shared_http_client = httpx.AsyncClient(
        limits=httpx.Limits(
            max_keepalive_connections=20,
            max_connections=100,
            keepalive_expiry=30.0,
        ),
        timeout=LLM_HTTPX_TIMEOUT,
    )

    # 6. main_graph：惰性导入
    from app.domain.chat.graph import build_main_graph

    main_graph = build_main_graph()

    # 7. audit_graph：惰性导入
    from app.domain.audit.graph import build_audit_graph

    audit_graph = build_audit_graph()

    return RuntimeResources(
        settings=settings,
        db_engine=engine,
        db_session_factory=session_factory,
        audit_redis=audit_redis,
        arq_pool=arq_pool,
        shared_http_client=shared_http_client,
        main_graph=main_graph,
        audit_graph=audit_graph,
    )


async def teardown_runtime(rr: RuntimeResources) -> None:
    """反向关闭进程级资源。

    关闭顺序：`shared_http_client` → `arq_pool` → `audit_redis` → `db_engine`，
    与构建顺序相反。LangGraph 编译产物无需显式关闭。
    `shared_http_client` 必须先于 DB / Redis 关：进程退出前的所有 LLM
    流可能仍在用池里的 keep-alive 连接，httpx `aclose` 会 drain 完挂起
    请求再退出。

    Args:
        rr: 由 `build_runtime` 创建的 `RuntimeResources`。
    """
    await rr.shared_http_client.aclose()
    await rr.arq_pool.aclose(close_connection_pool=True)
    await rr.audit_redis.aclose()
    await rr.db_engine.dispose()
