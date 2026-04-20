import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import UserRole


class TokenPayload(BaseModel):
    """Redis 缓存与 DB 查询的统一返回形状。"""
    user_id: uuid.UUID
    role: UserRole
    family_id: uuid.UUID
    device_id: str  # 随 token 绑定；中间件比对 X-Device-Id header 免查 DB
    expires_at: Optional[datetime]
    # ISO date（北京时间）上一次滚动续期的日期；每日首次命中时触发 DB UPDATE
    last_rolled_date: Optional[str] = None


REDIS_KEY_PREFIX = "auth:"
REDIS_TTL_SECONDS = 600


def token_hash(token: str) -> str:
    """sha256 hex digest。入 DB 和 Redis key 的前缀哈希。"""
    raise NotImplementedError


async def issue_token(
    db: AsyncSession,
    user_id: uuid.UUID,
    role: UserRole,
    family_id: uuid.UUID,
    device_id: str,  # 必填：写入 auth_tokens.device_id (NOT NULL) + Redis payload
    *,
    ttl_days: Optional[int] = 7,  # None = 永不过期（child）
    device_info: Optional[dict] = None,
) -> str:
    """签新 token：DB 写 auth_tokens + stage Redis setex + 返回明文 token（仅此一次）。
    调用方在 issue_token 之前调 revoke_all_active_tokens 以保证「一次一设备」语义；
    调用链末尾必须 `await commit_with_redis(db, redis)` 才真正落盘并刷 Redis。"""
    raise NotImplementedError


async def resolve_token(
    db: AsyncSession,
    redis: Redis,
    token: str,
) -> Optional[TokenPayload]:
    """纯读：Redis 命中刷 TTL 返回；miss 查 DB 回填 Redis。不做 DB UPDATE；
    续期由 get_current_account 在判断 needs_roll 后显式调 roll_token_expiry。
    已吊销 / 已过期返回 None。"""
    raise NotImplementedError


def needs_roll(payload: TokenPayload) -> bool:
    """父 token 是否需要今天首次续期。子 token（expires_at=None）永远返 False。"""
    raise NotImplementedError


async def roll_token_expiry(
    db: AsyncSession, *, token_hash_hex: str, payload: TokenPayload,
) -> TokenPayload:
    """DB UPDATE expires_at +7d + stage Redis setex 新 payload；返回新 payload。
    调用方必须紧跟 `await commit_with_redis(db, redis)`。"""
    raise NotImplementedError


async def revoke_token(
    db: AsyncSession,
    token: str,
) -> None:
    """主动吊销单个 token：DB auth_tokens.revoked_at = NOW() + stage Redis delete。幂等。
    调用方必须紧跟 `await commit_with_redis(db, redis)`。"""
    raise NotImplementedError


async def revoke_all_active_tokens(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> int:
    """批量吊销指定用户的全部活跃 token（DB + Redis 同步清）。

    用途：
      - parent 新设备登录前（/auth/login issue_token 之前）
      - child 新设备扫码前（/auth/redeem-bind-token issue_token 之前）
      - 父端「下线所有设备」按钮（POST /children/{id}/revoke-tokens）
      - 运维 reset_parent_password 脚本

    返回被吊销的 token 数量。对无活跃 token 的 user 幂等返回 0。
    调用方必须紧跟 `await commit_with_redis(db, redis)`。
    """
    raise NotImplementedError
