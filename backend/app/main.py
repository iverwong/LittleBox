"""FastAPI 应用工厂与 lifespan 管理。

`create_app` 构造 `FastAPI` 实例、注册 CORS 中间件、挂载 `api/*` 路由。
`lifespan` 协调启动与关闭顺序，启动期先建立 Redis 连接池，再装配
`RuntimeResources` 写入 `app.state.resources`，关闭期先等候活跃 chat
后台任务优雅退出，再反向关闭进程级资源。
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.bind_tokens import router as bind_tokens_router
from app.api.children import router as children_router
from app.api.health import router as health_router
from app.api.me import router as me_router
from app.core.config import settings
from app.core.redis import redis_lifespan
from app.core.runtime import build_runtime, teardown_runtime

if TYPE_CHECKING:
    from app.core.runtime import RuntimeResources

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """启动与关闭的统一编排。

    startup 顺序：主 Redis 连接池 → `RuntimeResources` → 写入
    `app.state.resources`。
    shutdown 顺序：`RuntimeResources` 内的活跃 chat 后台任务等候
    （30 秒超时则取消）→ 反向关闭 arq_pool / audit_redis /
    db_engine。

    测试可通过预注入 `app.state.resources` 跳过 lifespan 实际初始化，
    此时仍走 shutdown 等候路径。
    """
    rr: RuntimeResources | None = getattr(app.state, "resources", None)
    if rr is not None:
        # 测试注入路径：不裹 redis_lifespan，不调 teardown
        try:
            yield
        finally:
            await _shutdown_wait(rr)
    else:
        # 生产路径：redis_lifespan 裹住 yield（保证 get_redis 在服务期可用）
        async with redis_lifespan():
            rr = await build_runtime(settings)
            app.state.resources = rr
            try:
                yield
            finally:
                try:
                    await _shutdown_wait(rr)
                finally:
                    await teardown_runtime(rr)


async def _shutdown_wait(rr: "RuntimeResources") -> None:
    """有界等候活跃 chat 后台任务优雅退出，超时则取消。

    生产路径与测试注入路径共用，确保 lifespan shutdown 阶段不会因为
    未完成的 chat 流而留下悬挂协程。

    Args:
        rr: 进程级资源容器，从中读取 chat 任务登记表。
    """
    tasks = list(rr._chat_tasks.values())
    if tasks:
        logger.warning(
            "waiting %d chat task(s) to finish (timeout=30s)",
            len(tasks),
        )
        done, pending = await asyncio.wait(tasks, timeout=30.0)
        if pending:
            logger.warning(
                "chat tasks timeout, cancelling %d task(s)",
                len(pending),
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


def create_app() -> FastAPI:
    """构造 FastAPI 应用实例。

    依据 `settings.debug` 决定是否暴露 Swagger UI（`/docs`）与
    Redoc（`/redoc`）；按 health / auth / children / bind_tokens /
    me 顺序注册路由。

    Returns:
        配置好的 FastAPI 应用实例，模块底部已据此实例化为 `app`。
    """
    application = FastAPI(
        title=settings.app_name,
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(health_router)
    application.include_router(auth_router)
    application.include_router(children_router)
    application.include_router(bind_tokens_router)
    application.include_router(me_router)
    return application


app = create_app()
