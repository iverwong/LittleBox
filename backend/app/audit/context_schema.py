"""审查图 per-run 不可变上下文（Runtime[AuditContextSchema]）。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from app.config import Settings


@dataclass(frozen=True)
class AuditContextSchema:
    """审查图单次运行的不可变上下文。

    与 RuntimeResources（进程级）的分工：RuntimeResources 承载容器级
    共享资源（engine / pool 等），AuditContextSchema 承载单次图调用
    所需的请求级上下文。二者均 frozen=True，运行时不可变。
    """
    # 身份字段
    session_id: uuid.UUID     # 被审查的对话 session UUID
    child_user_id: uuid.UUID  # 被审查的青少年用户 ID
    # 业务字段
    max_iter: int             # tool agentic loop 硬上限（D-patch0-1：不引入 target_message_id）
    # 三资源
    settings: Settings                    # 应用配置
    db_session_factory: async_sessionmaker[AsyncSession]  # DB 会话工厂
    audit_redis: Redis                    # 审查信号管道 Redis
