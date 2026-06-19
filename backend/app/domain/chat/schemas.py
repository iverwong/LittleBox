"""Request/response schemas for /me/sessions 与 chat stream 接口。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SessionListItem(BaseModel):
    """会话列表单条记录。

    Attributes:
        id: 会话 UUID。
        title: 会话标题(可空)。
        last_active_at: 最近活跃时间(用于排序)。
    """

    id: uuid.UUID
    title: str | None
    last_active_at: datetime


class SessionListResponse(BaseModel):
    """GET /me/sessions 响应。

    Attributes:
        sessions: 当前分页的会话条目列表。
        today_session_id: 今日会话 id(由 hard cut + 凌晨空闲 30min 软切策略判定),无则 None。
        next_cursor: 下一分页游标(URL-safe base64),无下一页则 None。
    """

    sessions: list[SessionListItem]
    today_session_id: uuid.UUID | None = None
    next_cursor: str | None


class MessageListItem(BaseModel):
    """消息列表单条记录。

    Attributes:
        id: 消息 UUID。
        role: 消息角色,取值 `human` 或 `ai`(对齐 LangChain HumanMessage/AIMessage)。
        content: 消息正文。
        status: 消息状态,取值 `active` 或 `compressed`。
        finish_reason: LLM 终止原因,可空。
        created_at: 创建时间(用于排序)。
    """

    id: uuid.UUID
    role: Literal["human", "ai"]
    content: str
    status: Literal["active", "compressed"]
    finish_reason: str | None
    created_at: datetime


class MessageListResponse(BaseModel):
    """GET /me/sessions/{id}/messages 响应。

    Attributes:
        items: 当前分页的消息条目列表(倒序)。
        next_cursor: 下一分页游标,无下一页则 None。
        in_progress: 是否存在对应 session 的进行中流(由 Redis chat lock 探测)。
    """

    items: list[MessageListItem]
    next_cursor: str | None
    in_progress: bool


class ChatStreamRequest(BaseModel):
    """POST /me/chat/stream 请求体。

    Attributes:
        content: 用户消息正文。允许为空字符串,仅在 `regenerate_for` 指向孤儿 human
            行(行 6 复用路径)时出现,其他路径传空将被判为非法。
        session_id: 服务端按日切策略强制重判生效 sid,此处仅为前端 hint;不一致时通过
            SSE session_meta.session_id 回灌生效 sid。null 表示首轮,非 null 表示已存在
            会话 id hint。
        regenerate_for: 必须等于该 session 当前最后一条 active human 消息的 id。指向
            早前 human / 任何 ai / 不存在的行 → 400 RegenerateForInvalid。
    """

    content: str = Field(
        description="用户消息正文。允许为空字符串,"
        "仅在 regenerate_for 指向孤儿 human 行(行 6 复用路径)时出现,其他路径传空将被判为非法。"
    )
    session_id: str | None = Field(
        default=None,
        description=(
            "前端 hint;服务端按日切策略强制重判生效 sid,"
            "不一致时通过 SSE session_meta.session_id 回灌生效 sid。"
            "null = 首轮;非 null = 已存在会话 id hint。"
        ),
    )
    regenerate_for: str | None = Field(
        default=None,
        description=(
            "必须等于该 session 当前最后一条 active human 消息的 id。"
            "指向早前 human / 任何 ai / 不存在的行 → 400 RegenerateForInvalid。"
        ),
    )
