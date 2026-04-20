import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.redis_ops import RedisOp, stage_redis_op
from app.models.accounts import AuthToken, User
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

_CST = ZoneInfo("Asia/Shanghai")


def _today_cst() -> str:
    return datetime.now(_CST).date().isoformat()


def _redis_key(th: str) -> str:
    return f"{REDIS_KEY_PREFIX}{th}"


def token_hash(token: str) -> str:
    """sha256 hex digest。入 DB 和 Redis key 的前缀哈希。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def issue_token(
    db: AsyncSession,
    user_id: uuid.UUID,
    role: UserRole,
    family_id: uuid.UUID,
    device_id: str,
    *,
    ttl_days: Optional[int] = 7,
    device_info: Optional[dict] = None,
) -> str:
    """签新 token：DB 写 auth_tokens + stage Redis setex + 返回明文 token（仅此一次）。
    调用方在 issue_token 之前调 revoke_all_active_tokens 以保证「一次一设备」语义；
    调用链末尾必须 `await commit_with_redis(db, redis)` 才真正落盘并刷 Redis。"""
    token = secrets.token_urlsafe(32)
    th = token_hash(token)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=ttl_days)
        if ttl_days is not None else None
    )
    db.add(AuthToken(
        user_id=user_id, token_hash=th, expires_at=expires_at,
        device_id=device_id, device_info=device_info,
    ))
    await db.flush()
    payload = TokenPayload(
        user_id=user_id, role=role, family_id=family_id,
        device_id=device_id, expires_at=expires_at,
        last_rolled_date=_today_cst() if expires_at is not None else None,
    )
    stage_redis_op(db, RedisOp(
        kind="setex", key=_redis_key(th),
        ttl_seconds=REDIS_TTL_SECONDS,
        value=payload.model_dump_json(),
    ))
    return token


async def resolve_token(
    db: AsyncSession, redis: Redis, token: str,
) -> Optional[TokenPayload]:
    """纯读路径：不做 DB UPDATE；续期由 get_current_account 调 roll_token_expiry。"""
    th = token_hash(token)
    cached = await redis.get(_redis_key(th))
    if cached is not None:
        payload = TokenPayload.model_validate_json(cached)
        if payload.expires_at is not None and payload.expires_at < datetime.now(timezone.utc):
            return None
        # 读路径 cache 维护：刷 TTL；失败下次 miss 自愈，不属业务状态
        await redis.expire(_redis_key(th), REDIS_TTL_SECONDS)
        return payload

    # Redis miss → 查 DB → 回填 Redis
    stmt = (
        select(AuthToken, User)
        .join(User, User.id == AuthToken.user_id)
        .where(AuthToken.token_hash == th, AuthToken.revoked_at.is_(None))
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        return None
    tok, user = row
    if tok.expires_at is not None and tok.expires_at < datetime.now(timezone.utc):
        return None

    # last_rolled_date 初始化为 (expires_at - 7d).date()，让外层 needs_roll
    # 能触发今天的首次续期（若今天尚未续）
    seed_date = (
        (tok.expires_at - timedelta(days=7)).astimezone(_CST).date().isoformat()
        if tok.expires_at is not None else None
    )
    payload = TokenPayload(
        user_id=user.id, role=user.role, family_id=user.family_id,
        device_id=tok.device_id, expires_at=tok.expires_at,
        last_rolled_date=seed_date,
    )
    # 读路径回填：不经 staging；失败下次 miss 重试
    await redis.setex(_redis_key(th), REDIS_TTL_SECONDS, payload.model_dump_json())
    return payload


def needs_roll(payload: TokenPayload) -> bool:
    return payload.expires_at is not None and payload.last_rolled_date != _today_cst()


async def roll_token_expiry(
    db: AsyncSession, *, token_hash_hex: str, payload: TokenPayload,
) -> TokenPayload:
    new_expires = datetime.now(timezone.utc) + timedelta(days=7)
    await db.execute(update(AuthToken).where(
        AuthToken.token_hash == token_hash_hex,
        AuthToken.revoked_at.is_(None),
    ).values(expires_at=new_expires))
    new_payload = payload.model_copy(update={
        "expires_at": new_expires,
        "last_rolled_date": _today_cst(),
    })
    stage_redis_op(db, RedisOp(
        kind="setex", key=_redis_key(token_hash_hex),
        ttl_seconds=REDIS_TTL_SECONDS,
        value=new_payload.model_dump_json(),
    ))
    return new_payload


async def revoke_token(db: AsyncSession, token: str) -> None:
    th = token_hash(token)
    await db.execute(update(AuthToken).where(
        AuthToken.token_hash == th, AuthToken.revoked_at.is_(None),
    ).values(revoked_at=datetime.now(timezone.utc)))
    stage_redis_op(db, RedisOp(kind="delete", key=_redis_key(th)))


async def revoke_all_active_tokens(db: AsyncSession, user_id: uuid.UUID) -> int:
    hashes = list((await db.execute(
        select(AuthToken.token_hash).where(
            AuthToken.user_id == user_id, AuthToken.revoked_at.is_(None),
        )
    )).scalars().all())
    if not hashes:
        return 0
    await db.execute(update(AuthToken).where(
        AuthToken.user_id == user_id, AuthToken.revoked_at.is_(None),
    ).values(revoked_at=datetime.now(timezone.utc)))
    for th in hashes:
        stage_redis_op(db, RedisOp(kind="delete", key=_redis_key(th)))
    return len(hashes)
