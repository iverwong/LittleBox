"""accounts 域 Pydantic 模型。

聚合账号上下文、child profile 输入输出与列表响应。auth 域的
`LoginResponse` 通过 `from app.domain.auth.schemas import AccountOut`
反向引用本模块,属 D-1 允许的 domain 间通过 schemas 单向通信用法。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.core.enums import Gender, UserRole
from app.core.time import age_at

if TYPE_CHECKING:
    from app.domain.accounts.models import ChildProfile


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
        nickname: 家长设置的子女昵称,长度 1-12。
        age: 子女年龄,合法范围 3-21 岁。
        gender: 性别,枚举值 male / female / unknown。
    """

    nickname: str = Field(min_length=1, max_length=12, description="家长设置的子女昵称")
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
    gender: Gender
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


class PutChildProfileRequest(BaseModel):
    """PUT /api/v1/child-profiles/{child_user_id} 请求体(全量提交语义)。

    每次携带全部可编辑字段。必输:nickname / birth_date / gender /
    sensitivity。可空(选填):concerns / custom_redlines,空串归一为 None
    = 清空。不收 age:年龄展示由前端用 birth_date 本地换算。

    Attributes:
        nickname: 家长设置的子女昵称,长度 1-12。
        birth_date: 出生日期;服务端按 Asia/Shanghai 时区即时换算整岁,
            越界 [3, 21] → 422。
        gender: 性别枚举。
        concerns: 家长自然语言描述的关注点,空串归一为 None。
        sensitivity: 6 维度关注度配置,整体替换。
        custom_redlines: 家长自定义红线话题,空串归一为 None。
    """

    nickname: str = Field(min_length=1, max_length=12)
    birth_date: date
    gender: Gender
    sensitivity: SensitivityConfig
    concerns: Optional[str] = Field(None, max_length=500)
    custom_redlines: Optional[str] = Field(None, max_length=500)

    @field_validator("birth_date")
    @classmethod
    def _age_in_range(cls, v: date) -> date:
        """birth_date 换算整岁必须 ∈ [3, 21],否则 422。

        与 `app.core.time.age_at` 对齐时区,前端拿到 birth_date 后本地
        用同一函数做显示换算。
        """
        age = age_at(v, tz="Asia/Shanghai")
        if not (3 <= age <= 21):
            raise ValueError("birth_date out of supported age range [3, 21]")
        return v

    @field_validator("concerns", "custom_redlines")
    @classmethod
    def _blank_to_none(cls, v: Optional[str]) -> Optional[str]:
        """空串 / 纯空格归一为 None,落库即清空(DB 列允许 NULL)。"""
        if v is None:
            return None
        v = v.strip()
        return v or None


class ChildProfileDetail(BaseModel):
    """GET / PUT /api/v1/child-profiles/{child_user_id} 响应体(父端全字段)。

    Attributes:
        child_user_id: 子用户 UUID。
        nickname: 子女昵称。
        gender: 性别枚举。
        birth_date: 出生日期。
        concerns: 家长自然语言描述的关注点,可空。
        sensitivity: 6 维度关注度配置,可空;读回经 `SensitivityConfig` 规整。
        custom_redlines: 家长自定义红线话题,可空。
    """

    child_user_id: uuid.UUID
    nickname: str
    gender: Gender
    birth_date: date
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

    @classmethod
    def from_profile(cls, profile: ChildProfile) -> ChildProfileSnapshot:
        """从 `ChildProfile` ORM 派生 snapshot,收口 age_at 换算与字段映射。

        与 `me.py` / `expert/worker.py` 共享唯一构造入口,避免字段映射散落;
        跨进程序列化(`usecase.enqueue_audit` 走 `asdict`)仍按字段名解包,
        字段增减需保持向后兼容(默认值兜底)。

        Args:
            profile: 已加载的 `ChildProfile` ORM 对象。

        Returns:
            填好全部字段的 `ChildProfileSnapshot`(frozen)。
        """
        return cls(
            child_user_id=profile.child_user_id,
            nickname=profile.nickname,
            gender=profile.gender.value,
            birth_date=profile.birth_date,
            age=age_at(profile.birth_date, tz="Asia/Shanghai"),
            sensitivity=profile.sensitivity,
            custom_redlines=profile.custom_redlines,
            concerns=profile.concerns,
        )
