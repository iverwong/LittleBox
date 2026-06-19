"""FastAPI async SQLAlchemy session 工厂 + ORM 基础。

业务 handler 走 commit_with_redis,不要在 yield 后显式 commit/close。
CLI 走 _common.cli_runtime(),应用层 get_db 使用 RuntimeResources 共享 engine。

D-1 边界:core/db.py 是零业务依赖叶子,不得 import 任何 model;
模型与 alembic 从 core.db 反向 import Base / BaseMixin。
6.B 迁入 Base / BaseMixin / naming_convention(原 app/models/base.py)。
6.D 起 core/models.py 聚合 domain/*/models.py,env.py 改引 core.models。
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
    """从进程级 RuntimeResources 获取会话,HTTP handler 与后台共用同一 engine。"""
    rr: RuntimeResources = request.app.state.resources
    async with rr.db_session_factory() as session:
        yield session


# ---------------------------------------------------------------------------
# ORM 基础类 + 公共字段混入(Phase 6.0 6.B 迁入,原 app/models/base.py)
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    pass


# 设置全局 naming convention(必须在模型定义之前生效)
# 所有 FK/Index/UniqueConstraint 将按此约定命名
Base.metadata.naming_convention = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# 模块级别名,shim 再导出用(transitional;与 Base.metadata.naming_convention
# 引用同一份 dict,改其一即改其二)
naming_convention = Base.metadata.naming_convention


class BaseMixin:
    """公共字段混入:id + created_at。"""

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
