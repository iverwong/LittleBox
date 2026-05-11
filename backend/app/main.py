"""FastAPI app factory."""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.bind_tokens import router as bind_tokens_router
from app.api.children import router as children_router
from app.api.health import router as health_router
from app.api.me import router as me_router
from app.auth.redis_client import redis_lifespan
from app.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """合并 lifespan：Redis 连接池。"""
    async with redis_lifespan():
        yield


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
