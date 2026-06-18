"""Account / child profile schemas (M4.8 / M5+)。

本模块为 accounts 域 Pydantic 模型,聚合账号上下文、child profile 输入输出与列表响应。
auth 域 (LoginResponse) 通过 `from app.domain.auth.schemas import AccountOut` 反向引用本模块,
属 D-1 允许的 `domain/*` 间通过 schemas 单向通信用法。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.core.enums import UserRole


class AccountOut(BaseModel):
    """对外返回的账号信息。**严禁**包含 password_hash / admin_note。"""

    id: uuid.UUID
    role: UserRole
    family_id: uuid.UUID
    phone: Optional[str] = Field(
        default=None,
        description="MVP 阶段为 4 位小写字母；PNVS 上线后为真手机号",
    )
    is_active: bool


class CurrentAccount(BaseModel):
    """鉴权中间件注入到 handler 的轻量账号上下文。"""

    id: uuid.UUID
    role: UserRole
    family_id: uuid.UUID
    expires_at: Optional[datetime] = Field(default=None, description="None 表示永不过期（子账号）")


class CreateChildRequest(BaseModel):
    """POST /children 请求体。"""

    nickname: str = Field(min_length=1, max_length=32, description="家长设置的子女昵称")
    age: int = Field(ge=3, le=21, description="子女年龄（岁）")
    gender: Literal["male", "female", "unknown"] = Field(description="性别（必填，仅三值之一）")


class ChildSummary(BaseModel):
    """POST /children 响应体 / GET /children 列表项。"""

    id: uuid.UUID
    nickname: str
    birth_date: date  # 必填：age_to_birth_date 必返合法值
    gender: Literal["male", "female", "unknown"]
    is_bound: bool


class ListChildrenResponse(BaseModel):
    """GET /children 响应体。"""

    children: list[ChildSummary]


class ChildProfileOut(BaseModel):
    """GET /me/profile 响应体。child_user_id = child 的 User.id（≠ ChildProfile.id PK）。"""

    child_user_id: uuid.UUID
    nickname: str
    gender: Literal["male", "female", "unknown"]
    birth_date: date


@dataclass(frozen=True)
class ChildProfileSnapshot:
    """跨域传输的 child profile 投影（chat / audit 共用）。

    与 ChildProfileOut 的关系：
    - ChildProfileOut: Pydantic BaseModel，HTTP 响应序列化
    - ChildProfileSnapshot: frozen dataclass，LangGraph runtime context 内部投影

    字段全非 Optional：与 ChildProfile ORM nullable=False 对齐。
    - age: 由 birth_date + tz 算得（me.py 边界算一次，所有 consumer 零开销读）
    - gender: 已是 ORM .value 字符串（与 ChildProfileOut 一致）
    """

    child_user_id: uuid.UUID
    nickname: str
    gender: str
    birth_date: date
    age: int
    sensitivity: dict | None
    custom_redlines: str | None
