"""FastAPI app factory."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

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
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """合并 lifespan：Redis 连接池 + 进程级 RuntimeResources。"""
    # M9-patch1 test seam: 若 app.state.resources 已预注入（测试缝），
    # 跳过 redis_lifespan + build_runtime + teardown_runtime，
    # 但 shutdown 等候块仍按真实路径运行（#5/#6 测试依赖）。
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
    """有界等候正在运行的 chat bg task 优雅退出。超时则 cancel。

    M9-patch1 从 lifespan 抽出的共享 helper，生产路径和测试路径复用。
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
    """应用工厂。"""
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
