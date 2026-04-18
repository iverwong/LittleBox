from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.dev_chat import router as dev_chat_router
from app.api.health import router as health_router
from app.config import settings


def create_app() -> FastAPI:
    """应用工厂。"""
    application = FastAPI(
        title=settings.app_name,
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(health_router)
    application.include_router(dev_chat_router)
    return application


app = create_app()
