"""Children 路由 schemas（M4.8 B3/B4）。"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class CreateChildRequest(BaseModel):
    """POST /children 请求体。"""

    nickname: str = Field(min_length=1, max_length=32, description="家长设置的子女昵称")
    age: int = Field(ge=3, le=21, description="子女年龄（岁）")
    gender: Literal["male", "female", "unknown"] = Field(
        description="性别（必填，仅三值之一）"
    )


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
    """GET /me/profile 响应体。id = child user id（ChildProfile.child_user_id）。"""

    id: uuid.UUID
    nickname: str
    gender: Literal["male", "female", "unknown"]
    birth_date: date
