"""Auth token 生命周期:issue / resolve / roll / revoke。

存储与缓存分工
--------------
- DB(`auth_tokens`):持久化 `token_hash`(sha256,非明文) + `device_id`
  + `expires_at` + `revoked_at`
- Redis(主 db=0,`auth:` 前缀):缓存 `TokenPayload` JSON,TTL 600s,热路径免查 DB
- 入 Redis 走 `stage_redis_op`,与 DB commit 在 `commit_with_redis` 里原子串联

业务契约
--------
- "一次一设备":登录成功后由调用方先 `revoke_all_active_tokens`,
  再 `issue_token`
- "按日续期":parent token(`expires_at` 非空)每日首次命中时,
  由 `get_current_account` 触发 `roll_token_expiry`
- "设备绑定":`payload.device_id` 与请求头 `X-Device-Id` 不一致时立即吊销 + 401(fail-closed)
"""

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

from app.core.enums import UserRole
from app.core.redis import RedisOp, stage_redis_op
from app.domain.accounts.models import AuthToken, User


class TokenPayload(BaseModel):
    """Redis 缓存与 DB 查询的统一返回形状。"""

    user_id: uuid.UUID
    role: UserRole
    family_id: uuid.UUID
    device_id: str  # 随 token 绑定；中间件比对 X-Device-Id header 免查 DB
    expires_at: Optional[datetime]
    # ISO date（北京时间）上一次滚动续期的日期；每日首次命中时触发 DB UPDATE
    last_rolled_date: Optional[str] = None


# Redis key 前缀,与其它域(auth: / audit: / chat: ...)隔开,避免 namespace 撞车
REDIS_KEY_PREFIX = "auth:"
# 600s = 10min。远小于 token 有效期(7d),resolve 时会续 TTL;
# 短 TTL 缩小泄露面 + 控制内存增长,长 TTL 降低 DB 压力。
REDIS_TTL_SECONDS = 600

# 续期"按日"用北京时间:业务侧"今天是否续过"按家庭所在地自然日算,
# 避免子端跨时区往返出现同日两次续期/漏续。
_CST = ZoneInfo("Asia/Shanghai")


def _today_cst() -> str:
    """返回今天(CST, ISO date 字符串)。续期判断的唯一时间源。"""
    return datetime.now(_CST).date().isoformat()


def _redis_key(th: str) -> str:
    """`auth:<token_hash>`,集中保证前缀一致。"""
    return f"{REDIS_KEY_PREFIX}{th}"


def token_hash(token: str) -> str:
    """sha256 hex digest。入 DB 和 Redis key 的前缀哈希。

    选择 sha256 而非 bcrypt/argon2:token 本身是 256bit 随机串(secrets.token_urlsafe),
    已经是密码学强度,DB 泄露也不可逆推出明文,无需再加盐慢哈希。
    """
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
    """签新 token:DB 写 auth_tokens + stage Redis setex + 返回明文 token(仅此一次)。

    契约:
    - 调用方在 issue_token 之前必须 `revoke_all_active_tokens`,以保证"一次一设备"
    - 调用链末尾必须 `await commit_with_redis(db, redis)`,DB 行与 Redis 缓存才一并落盘
    - 返回的明文 token 不会再次出现在系统里,丢失即需重新登录
    """
    token = secrets.token_urlsafe(32)
    th = token_hash(token)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=ttl_days) if ttl_days is not None else None
    )
    db.add(
        AuthToken(
            user_id=user_id,
            token_hash=th,
            expires_at=expires_at,
            device_id=device_id,
            device_info=device_info,
        )
    )
    await db.flush()
    payload = TokenPayload(
        user_id=user_id,
        role=role,
        family_id=family_id,
        device_id=device_id,
        expires_at=expires_at,
        last_rolled_date=_today_cst() if expires_at is not None else None,
    )
    stage_redis_op(
        db,
        RedisOp(
            kind="setex",
            key=_redis_key(th),
            ttl_seconds=REDIS_TTL_SECONDS,
            value=payload.model_dump_json(),
        ),
    )
    return token


async def resolve_token(
    db: AsyncSession,
    redis: Redis,
    token: str,
) -> Optional[TokenPayload]:
    """纯读路径:不做 DB UPDATE;续期由 `get_current_account` 调 `roll_token_expiry`。

    流程:
    1. Redis 命中 → 校验 `expires_at` 未过期 → 刷 TTL → 返回
       (刷 TTL 失败不属业务状态,下次 miss 自愈)
    2. Redis miss → 查 DB(`auth_tokens` join `users`,过滤已撤销)
       → 重建 `TokenPayload` → 直接 `setex` 回填 Redis(不经 staging,
       失败下次 miss 重试)→ 返回
    3. 任意一步查到/缓存到但已过期 → 返回 `None`(由 deps 抛 401)

    返回:
    - `TokenPayload`:token 有效
    - `None`:token 不存在、已撤销、或已过期
    """
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
        if tok.expires_at is not None
        else None
    )
    payload = TokenPayload(
        user_id=user.id,
        role=user.role,
        family_id=user.family_id,
        device_id=tok.device_id,
        expires_at=tok.expires_at,
        last_rolled_date=seed_date,
    )
    # 读路径回填：不经 staging；失败下次 miss 重试
    await redis.setex(_redis_key(th), REDIS_TTL_SECONDS, payload.model_dump_json())
    return payload


def needs_roll(payload: TokenPayload) -> bool:
    """判断 parent token 是否需要滚动续期。

    语义:
    - `expires_at is None`(子端永久 token)→ 永不需要续
    - `last_rolled_date == 今天` → 今天已续过,跳过
    - 其余(parent 且今天首次命中) → 需要续
    """
    return payload.expires_at is not None and payload.last_rolled_date != _today_cst()


async def roll_token_expiry(
    db: AsyncSession,
    *,
    token_hash_hex: str,
    payload: TokenPayload,
) -> TokenPayload:
    """滚动续期:DB UPDATE expires_at += 7d + stage Redis 覆盖新 payload。

    契约:
    - 调用方(典型为 `get_current_account`)在 `needs_roll` 为真时调用
    - 调用链末尾必须 `await commit_with_redis(db, redis)` 才落盘
    - 返回新 payload(已含新 `expires_at` 与今天的 `last_rolled_date`),
      避免外层再 `model_copy`
    """
    new_expires = datetime.now(timezone.utc) + timedelta(days=7)
    await db.execute(
        update(AuthToken)
        .where(
            AuthToken.token_hash == token_hash_hex,
            AuthToken.revoked_at.is_(None),
        )
        .values(expires_at=new_expires)
    )
    new_payload = payload.model_copy(
        update={
            "expires_at": new_expires,
            "last_rolled_date": _today_cst(),
        }
    )
    stage_redis_op(
        db,
        RedisOp(
            kind="setex",
            key=_redis_key(token_hash_hex),
            ttl_seconds=REDIS_TTL_SECONDS,
            value=new_payload.model_dump_json(),
        ),
    )
    return new_payload


async def revoke_token(db: AsyncSession, token: str) -> None:
    """撤销单个 token:DB 标 `revoked_at` + stage Redis delete。

    幂等:已撤销的 token 再调一次是 no-op(`revoked_at.is_(None)` 过滤)。
    调用链末尾必须 `await commit_with_redis(db, redis)` 才生效。
    """
    th = token_hash(token)
    await db.execute(
        update(AuthToken)
        .where(
            AuthToken.token_hash == th,
            AuthToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(timezone.utc))
    )
    stage_redis_op(db, RedisOp(kind="delete", key=_redis_key(th)))


async def revoke_all_active_tokens(db: AsyncSession, user_id: uuid.UUID) -> int:
    """撤销某用户所有未撤销的 token,返回被撤销的 token 数量。

    两步式:
    1. 先 SELECT 拿到所有 token_hash(用于后续批量删 Redis key)
    2. 再 UPDATE 标 `revoked_at`,然后循环 stage Redis delete
    —— 不直接 SELECT-then-UPDATE 拼一条 SQL,是因为 UPDATE 返回的影响行
    不会带原 token_hash,Redis delete 需要 hash 才能拼 key。

    用途:登录成功后的"一次一设备"语义(配合 `issue_token` 使用)。
    """
    hashes = list(
        (
            await db.execute(
                select(AuthToken.token_hash).where(
                    AuthToken.user_id == user_id,
                    AuthToken.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not hashes:
        return 0
    await db.execute(
        update(AuthToken)
        .where(
            AuthToken.user_id == user_id,
            AuthToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(timezone.utc))
    )
    for th in hashes:
        stage_redis_op(db, RedisOp(kind="delete", key=_redis_key(th)))
    return len(hashes)
