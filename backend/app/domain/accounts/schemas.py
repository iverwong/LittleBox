"""accounts 域 Pydantic 模型。

聚合账号上下文、child profile 输入输出与列表响应。auth 域的
`LoginResponse` 通过 `from app.domain.auth.schemas import AccountOut`
反向引用本模块,属 D-1 允许的 domain 间通过 schemas 单向通信用法。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.core.enums import UserRole


class AccountOut(BaseModel):
    """对外返回的账号信息。

    严禁包含 `password_hash` / `admin_note` 等敏感字段。

    Attributes:
        id: 用户 UUID。
        role: 用户角色。
        family_id: 所属家庭 UUID。
        phone: 手机号,父账号字段。
        is_active: 是否启用。
    """

    id: uuid.UUID
    role: UserRole
    family_id: uuid.UUID
    phone: Optional[str] = Field(
        default=None,
        description="父账号手机号",
    )
    is_active: bool


class CurrentAccount(BaseModel):
    """鉴权中间件注入到 handler 的轻量账号上下文。

    Attributes:
        id: 用户 UUID。
        role: 用户角色。
        family_id: 所属家庭 UUID。
        expires_at: token 过期时间,`None` 表示永不过期(子账号)。
    """

    id: uuid.UUID
    role: UserRole
    family_id: uuid.UUID
    expires_at: Optional[datetime] = Field(default=None, description="None 表示永不过期(子账号)")


class CreateChildRequest(BaseModel):
    """POST /children 请求体。

    Attributes:
        nickname: 家长设置的子女昵称,长度 1-32。
        age: 子女年龄,合法范围 3-21 岁。
        gender: 性别,枚举值 male / female / unknown。
    """

    nickname: str = Field(min_length=1, max_length=32, description="家长设置的子女昵称")
    age: int = Field(ge=3, le=21, description="子女年龄(岁)")
    gender: Literal["male", "female", "unknown"] = Field(description="性别(必填,仅三值之一)")


class ChildSummary(BaseModel):
    """POST /children 响应体 / GET /children 列表项。

    Attributes:
        id: 子账号 User.id。
        nickname: 子女昵称。
        birth_date: 出生日期,`age_to_birth_date` 必返合法值。
        gender: 性别。
        is_bound: 是否已绑定(存在有效 AuthToken)。
    """

    id: uuid.UUID
    nickname: str
    birth_date: date
    gender: Literal["male", "female", "unknown"]
    is_bound: bool


class ListChildrenResponse(BaseModel):
    """GET /children 响应体。

    Attributes:
        children: 该父账号家庭下的全部子账号摘要列表。
    """

    children: list[ChildSummary]


class ChildProfileOut(BaseModel):
    """GET /me/profile 响应体。

    `child_user_id` 是 child 对应的 `User.id`(不同于 `ChildProfile.id` 主键)。

    Attributes:
        child_user_id: 子用户 UUID。
        nickname: 子女昵称。
        gender: 性别。
        birth_date: 出生日期。
    """

    child_user_id: uuid.UUID
    nickname: str
    gender: Literal["male", "female", "unknown"]
    birth_date: date


class SensitivityConfig(BaseModel):
    """家长对 6 个风险维度的关注度配置(1–9,默认 5)。

    数值语义对齐 audit/prompts.py 的 LEVEL_MAP(1=完全不关注 … 5=正常关注 …
    9=极度关注):配置越高 = 该维度家长越关注、审查越严;越低 = 越宽容。作为
    前端 / API / 审查 prompt 共用 6 个 key 的单一事实源。

    Attributes:
        emotional: 情绪与心理。
        social: 人际与社交。
        values: 价值观与世界观。
        boundaries: AI 应用边界。
        academic: 学习独立性。
        lifestyle: 生活方式。
    """

    emotional: int = Field(5, ge=1, le=9, description="情绪与心理")
    social: int = Field(5, ge=1, le=9, description="人际与社交")
    values: int = Field(5, ge=1, le=9, description="价值观与世界观")
    boundaries: int = Field(5, ge=1, le=9, description="AI 应用边界")
    academic: int = Field(5, ge=1, le=9, description="学习独立性")
    lifestyle: int = Field(5, ge=1, le=9, description="生活方式")


class UpdateChildProfileRequest(BaseModel):
    """PATCH /api/v1/child-profiles/{child_user_id} 请求体(部分更新)。

    全字段 Optional:未传 = 不动。可空字段(concerns / custom_redlines /
    sensitivity)传 null = 清空;非空字段(nickname / age / gender)传 null 视为
    不动(DB NOT NULL,语义不允许清空)。sensitivity 为整体替换(提交完整
    6 维),不做维度级 merge。

    Attributes:
        nickname: 家长设置的子女昵称,长度 1-32。
        age: 子女年龄,合法范围 3-21 岁;后端重算 birth_date。
        gender: 性别,枚举值 male / female / unknown。
        concerns: 家长自然语言描述的关注点,空串视为清空。
        sensitivity: 6 维度关注度配置,整体替换。
        custom_redlines: 家长自定义红线话题,空串视为清空。
    """

    nickname: Optional[str] = Field(None, min_length=1, max_length=32)
    age: Optional[int] = Field(None, ge=3, le=21)
    gender: Optional[Literal["male", "female", "unknown"]] = None
    concerns: Optional[str] = None
    sensitivity: Optional[SensitivityConfig] = None
    custom_redlines: Optional[str] = None


class ChildProfileDetail(BaseModel):
    """GET / PATCH /api/v1/child-profiles/{child_user_id} 响应体(父端全字段)。

    Attributes:
        child_user_id: 子用户 UUID。
        nickname: 子女昵称。
        gender: 性别。
        birth_date: 出生日期。
        age: 由 `birth_date` + 时区即时换算的整岁数(消费方零开销读)。
        concerns: 家长自然语言描述的关注点,可空。
        sensitivity: 6 维度关注度配置,可空;读回经 `SensitivityConfig` 规整。
        custom_redlines: 家长自定义红线话题,可空。
    """

    child_user_id: uuid.UUID
    nickname: str
    gender: Literal["male", "female", "unknown"]
    birth_date: date
    age: int
    concerns: Optional[str]
    sensitivity: Optional[SensitivityConfig]
    custom_redlines: Optional[str]


@dataclass(frozen=True)
class ChildProfileSnapshot:
    """跨域传输的 child profile 投影,chat 与 audit 共用。

    与 `ChildProfileOut` 的区分:
    - `ChildProfileOut` 为 Pydantic BaseModel,负责 HTTP 响应序列化。
    - `ChildProfileSnapshot` 为 frozen dataclass,作为 LangGraph runtime
      context 的内部投影在 chat / audit 域之间传递。

    字段全非 Optional,与 ChildProfile ORM 的 `nullable=False` 约束对齐。
    `gender` 已是 ORM `.value` 字符串(与 ChildProfileOut 一致)。

    Attributes:
        child_user_id: 子用户 UUID。
        nickname: 子女昵称。
        gender: 性别字符串。
        birth_date: 出生日期。
        age: 由 `birth_date` + 时区即时换算的整岁数(消费方零开销读)。
        sensitivity: 6 维度敏感度 JSON,可空。
        custom_redlines: 家长自定义红线文本,可空。
        concerns: 家长自然语言描述的关注点,注入审查 prompt(仅 audit)。
    """

    child_user_id: uuid.UUID
    nickname: str
    gender: str
    birth_date: date
    age: int
    sensitivity: Optional[dict]
    custom_redlines: Optional[str]
    concerns: Optional[str]
