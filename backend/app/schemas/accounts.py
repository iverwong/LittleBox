"""Account schemas for auth API."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.models.enums import Gender, UserRole


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
    expires_at: Optional[datetime] = Field(
        default=None, description="None 表示永不过期（子账号）"
    )


class LoginRequest(BaseModel):
    phone: str = Field(min_length=4, max_length=32)
    password: str = Field(
        min_length=8,
        max_length=128,
        description=(
            "MVP 特供；PNVS 上线后 LoginRequest 整体替换为短信验证流程（新增 "
            "/auth/login-sms 端点，body 改为 {phone, sms_code}），本字段随端点废弃"
        ),
    )
    device_id: str = Field(
        min_length=1,
        max_length=255,
        description="客户端 UUID v4；Expo SecureStore 持久化；设备变化即触发老 token 吊销",
    )


class LoginResponse(BaseModel):
    """登录成功响应。"""

    token: str = Field(description="Opaque token，43 字符 base64")
    account: AccountOut


class CreateChildRequest(BaseModel):
    nickname: Optional[str] = Field(default=None, max_length=50)
    birth_date: Optional[date] = None
    gender: Optional[Gender] = None


class BindTokenResponse(BaseModel):
    bind_token: str = Field(description="5 分钟 TTL，一次性使用")
    expires_in_seconds: Literal[300] = 300


class CreateBindTokenRequest(BaseModel):
    """POST /bind-tokens：parent 为指定 child 创建绑定 token。"""

    child_user_id: uuid.UUID = Field(
        description="目标 child 的 user id；必须在当前 parent 的 family 内",
    )


class RedeemBindTokenRequest(BaseModel):
    """POST /bind-tokens/{bind_token}/redeem：子端扫码后兑换 token。"""

    device_id: str = Field(
        min_length=1,
        max_length=255,
        description="子端设备 UUID；持久化到 auth_tokens.device_id（NOT NULL）",
    )


class BindTokenStatusOut(BaseModel):
    """父端轮询子端扫码状态用。MVP 无推送基座（推送在 M10/M11），
    父端 QR 页面走 5 秒轮询 GET /api/v1/bind-tokens/{bind_token}/status：
    - status="pending" → 子端尚未扫码（bind_token 还活着）
    - status="bound"   → 子端已扫码兑换成功，父端可自动跳转 + 记录 child_user_id
    - 404 not_found    → bind_token 已过期且未兑换，父端应重新生成
    查询纯走 Redis（O(1) 内存 GET），不打 DB。详见决策背景 §1.2。"""

    status: Literal["pending", "bound"]
    child_user_id: Optional[uuid.UUID] = Field(
        default=None, description="status=bound 时返回；pending 时为 None",
    )
    bound_at: Optional[datetime] = Field(
        default=None, description="status=bound 时子端完成兑换的 UTC 时间",
    )
