"""审查图 per-run 不可变上下文(Runtime[AuditContextSchema])。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.accounts.schemas import ChildProfileSnapshot

if TYPE_CHECKING:
    import httpx

    from app.core.config import Settings


@dataclass(frozen=True)
class AuditContextSchema:
    """审查图单次运行的不可变上下文。

    与 RuntimeResources(进程级)的分工:RuntimeResources 承载容器级共享资源
    (engine / pool / shared_http_client / CompiledStateGraph 等),
    AuditContextSchema 承载单次图调用所需的请求级上下文。二者均 frozen=True,
    运行时不可变。

    Attributes:
        session_id: 被审查的对话 session UUID。
        child_user_id: 被审查的青少年用户 ID。
        target_message_id: 被审查的 ai_msg id(本轮审查锚点,必非空)。
        max_iter: tool agentic loop 硬上限。
        child_profile: 孩子档案快照(用于 prompt 注入家长关注度与红线配置)。
        settings: 应用配置。
        db_session_factory: DB 会话工厂,worker 层负责注入。
        audit_redis: 审查信号管道 Redis。
        shared_http_client: 进程级共享 httpx 客户端,worker 层从
            `rr.shared_http_client` 注入(与 chat 路径同构)。
    """

    # 身份字段
    session_id: uuid.UUID  # 被审查的对话 session UUID
    child_user_id: uuid.UUID  # 被审查的青少年用户 ID
    target_message_id: uuid.UUID  # 被审查的 ai_msg id(本轮审查锚点,必非空)
    # 业务字段
    max_iter: int  # tool agentic loop 硬上限
    child_profile: ChildProfileSnapshot
    # 三资源
    settings: Settings  # 应用配置
    db_session_factory: async_sessionmaker[AsyncSession]  # DB 会话工厂
    audit_redis: Redis  # 审查信号管道 Redis
    shared_http_client: httpx.AsyncClient  # 进程级共享 httpx 客户端
