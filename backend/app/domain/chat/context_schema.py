"""主对话图 per-run 不可变上下文(Runtime[ChatContextSchema])。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.accounts.schemas import ChildProfileSnapshot

if TYPE_CHECKING:
    from app.core.config import Settings


@dataclass(frozen=True)
class ChatContextSchema:
    """主对话图单次运行的不可变上下文。

    与 RuntimeResources(进程级)的分工:RuntimeResources 承载容器级
    共享资源(engine / pool 等),ChatContextSchema 承载单次图调用所需
    的请求级上下文。二者均 frozen=True,运行时不可变。

    Attributes:
        session_id: 当前对话 session UUID。
        child_user_id: 当前请求归属的青少年用户 id。
        child_profile: 跨域 child 投影(chat / audit 共用)。
        user_input: 本轮用户输入原文;行 6 复用孤儿路径由调用方喂入原始问题文本。
        settings: 应用配置。
        db_session_factory: DB 会话工厂(供图内节点按需开短连接)。
        audit_redis: 审查信号管道 Redis。
    """

    # 身份字段
    session_id: uuid.UUID
    child_user_id: uuid.UUID
    # 业务字段
    child_profile: ChildProfileSnapshot
    user_input: str
    # 资源字段
    settings: Settings
    db_session_factory: async_sessionmaker[AsyncSession]
    audit_redis: Redis
