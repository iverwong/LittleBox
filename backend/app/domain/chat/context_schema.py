"""主对话图 per-run 不可变上下文（Runtime[ChatContextSchema]）。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from app.core.config import Settings


@dataclass(frozen=True)
class ChatContextSchema:
    """主对话图单次运行的不可变上下文。

    与 RuntimeResources（进程级）的分工：RuntimeResources 承载容器级
    共享资源（engine / pool 等），ChatContextSchema 承载单次图调用所需
    的请求级上下文。二者均 frozen=True，运行时不可变。
    """

    # 身份字段
    session_id: uuid.UUID  # 当前对话 session UUID
    child_user_id: uuid.UUID  # 当前请求归属的青少年用户 ID
    # 业务字段
    child_profile: dict[str, Any]  # 青少年档案快照（昵称等）
    age: int  # 计算后的年龄（compute_age 结果）
    gender: str | None  # 性别；None 表示未提供
    user_input: str  # 本轮用户输入原文；patch0 期未被任何节点消费
    # （D-patch0-5 删 build_messages_main 末位 append 后），
    # M9 主体期 §C.4 W1 模式 GUIDANCE_WRAPPER 重启用
    # 三资源
    settings: Settings  # 应用配置
    db_session_factory: async_sessionmaker[AsyncSession]  # DB 会话工厂
    audit_redis: Redis  # 审查信号管道 Redis
