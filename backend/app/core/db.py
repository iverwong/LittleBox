"""FastAPI 异步 SQLAlchemy session 工厂与 ORM 基础。

业务 handler 走 `commit_with_redis`，不在 yield 后显式 commit/close；
CLI 走 `app/scripts/_common.py::cli_runtime`，HTTP handler 通过
`Depends(get_db)` 共享 `RuntimeResources` 中的 engine。

本文件是 D-1 边界下的零业务依赖叶子：不得 import 任何 model；模型与
alembic 从 `app.core.db` 反向 import `Base` / `BaseMixin`。
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime

from fastapi import Request
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.runtime import RuntimeResources


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：从进程级 `RuntimeResources` 获取 `AsyncSession`。

    HTTP handler 与后台任务共用同一 engine，由 lifespan / ARQ on_startup
    通过 `build_runtime` 创建。

    Args:
        request: FastAPI 请求对象，从中取 `request.app.state.resources`。

    Yields:
        `AsyncSession`：handler 退出时由上下文管理器关闭。
    """
    rr: RuntimeResources = request.app.state.resources
    async with rr.db_session_factory() as session:
        yield session


# ---------------------------------------------------------------------------
# ORM 基础类 + 公共字段混入
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """所有 ORM 模型的统一基类（`DeclarativeBase` 子类）。

    配合下面的 `naming_convention`，所有 FK / Index / UniqueConstraint
    自动按约定命名，便于 alembic 生成的迁移可读且可重放。
    """

    pass


# 全局命名约定：必须在任何模型定义之前生效。
# 规则：FK/Index/UniqueConstraint 等约束的自动名按此模板拼出。
Base.metadata.naming_convention = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# 模块级名：与 `Base.metadata.naming_convention` 引用同一份 dict，
# 改其一即改其二。供外部按需引用。
naming_convention = Base.metadata.naming_convention


class BaseMixin:
    """公共字段混入：`id` + `created_at`。

    Attributes:
        id: 主键 UUID，服务端 `gen_random_uuid()` 生成。
        created_at: 创建时间戳（带时区），服务端 `now()` 生成，非空。
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
