"""auth / bind_token 协议层 Pydantic 模型。

聚合登录请求响应与 bind_token 完整生命周期(create / status / redeem)的入参出参。
本模块可被 `accounts` 域 `AccountOut` 反向引用,属边界规范允许的
domain 间通过 schemas 单向通信用法(auth 单向依赖 accounts,无反向)。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.domain.accounts.schemas import AccountOut


class LoginRequest(BaseModel):
    """POST /auth/login 请求体。

    Attributes:
        phone: 父账号登录标识;当前版本为 4 位小写字母临时方案。
        password: 父账号密码明文(走 argon2id 校验)。
        device_id: 客户端 UUID;登录成功后与 token 绑定,后续请求需在
            X-Device-Id 头携带同一值,变更即触发旧 token 吊销。
    """

    phone: str = Field(min_length=4, max_length=32)
    password: str = Field(
        min_length=8,
        max_length=128,
        description=(
            "父账号密码;短信验证上线后将由 /auth/login-sms 端点承接,"
            "body 改为 {phone, sms_code},本字段随端点废弃"
        ),
    )
    device_id: str = Field(
        min_length=1,
        max_length=255,
        description="客户端 UUID;Expo SecureStore 持久化;设备变化即触发老 token 吊销",
    )


class LoginResponse(BaseModel):
    """登录成功响应。

    Attributes:
        token: 不透明 token,43 字符 base64 url-safe 随机串。
        account: 当前账号公开信息,详见 accounts 域 `AccountOut`。
    """

    token: str = Field(description="Opaque token,43 字符 base64 url-safe")
    account: AccountOut


class BindTokenResponse(BaseModel):
    """POST /bind-tokens(create)响应体。

    返回给父端拿去生成 QR;bind_token 一次性,扫码 redeem 后立即失效,
    5 分钟内未 redeem 则 TTL 过期。父端 5 秒轮询 GET /bind-tokens/{bind_token}/status
    拿到 404 来切态,不在响应里下发剩余 TTL。

    Attributes:
        bind_token: 一次性绑定凭证;5 分钟 TTL。
    """

    bind_token: str = Field(description="5 分钟 TTL,一次性使用")


class CreateBindTokenRequest(BaseModel):
    """POST /bind-tokens 请求体。

    Attributes:
        child_user_id: 目标 child 的 user id;必须在当前 parent 的 family 内,
            由路由层做边界校验,本模型只做类型契约。
    """

    child_user_id: uuid.UUID = Field(
        description="目标 child 的 user id;必须在当前 parent 的 family 内",
    )


class RedeemBindTokenRequest(BaseModel):
    """POST /bind-tokens/{bind_token}/redeem 请求体。

    Attributes:
        device_id: 子端设备 UUID;持久化到 auth_tokens.device_id(列定义 NOT NULL)。
    """

    device_id: str = Field(
        min_length=1,
        max_length=255,
        description="子端设备 UUID;持久化到 auth_tokens.device_id(NOT NULL)",
    )


class BindTokenStatusOut(BaseModel):
    """GET /bind-tokens/{bind_token}/status 响应体。

    用于父端 QR 页面 5 秒轮询,确认子端扫码状态;不依赖推送通道。

    Attributes:
        status: 绑定状态。`pending` 表示子端尚未扫码(bind_token 仍存活);
            `bound` 表示子端已扫码兑换成功。bind_token 已过期且未兑换时,
            路由层直接返回 status.HTTP_404_NOT_FOUND 而非本响应体。
        child_user_id: 仅在 status=bound 时返回;pending 时为 None。
        bound_at: 仅在 status=bound 时返回;子端完成兑换的 UTC 时间。
    """

    status: Literal["pending", "bound"]
    child_user_id: Optional[uuid.UUID] = Field(
        default=None,
        description="status=bound 时返回;pending 时为 None",
    )
    bound_at: Optional[datetime] = Field(
        default=None,
        description="status=bound 时子端完成兑换的 UTC 时间",
    )
