"""AuthToken 生命周期管理:`issue` / `resolve` / `needs_roll` / `roll_token_expiry` / `revoke`。

存储分工
--------
- DB(`auth_tokens`):持久化 `token_hash`(sha256,非明文)、`device_id`、
  `expires_at`、`revoked_at` 等字段。
- Redis(主 db=0,`auth:` 前缀):缓存 `TokenPayload` JSON,TTL `REDIS_TTL_SECONDS`,
  解析热路径免查 DB。
- 入 Redis 走 `stage_redis_op`,与 DB commit 在 `commit_with_redis` 里原子串联;
  `resolve_token` 的命中刷 TTL 与 miss 回填是只读维护,直接走 Redis 不经 staging。

业务契约
--------
- 一次一设备:登录成功后调用方先 `revoke_all_active_tokens` 再 `issue_token`。
- 按日续期:parent token(`expires_at` 非空)每日首次命中时,由
  `get_current_account` 触发 `roll_token_expiry`,子 token 永不过期不续期。
- 设备绑定:`payload.device_id` 与请求头 X-Device-Id 不一致时立即吊销 + 401
  (fail-closed)。
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional

from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import UserRole
from app.core.redis import RedisOp, stage_redis_op
from app.core.time import SHANGHAI, now_shanghai, now_utc
from app.domain.accounts.models import AuthToken, User


class TokenPayload(BaseModel):
    """Redis 缓存与 DB 解析的统一返回形状。

    Attributes:
        user_id: token 所属用户的 UUID。
        role: 用户角色(枚举 `UserRole`),供 `get_current_account` 之外的消费方
            必要时使用。
        family_id: 所属家庭 UUID。
        device_id: 与 token 绑定的设备 UUID;`get_current_account` 用来与
            X-Device-Id 头比对。
        expires_at: UTC 过期时间;`None` 表示永不过期(子端 token)。
        last_rolled_date: 上次滚动续期的北京时区 ISO date;每日首次命中时触发
            DB UPDATE。
    """

    user_id: uuid.UUID
    role: UserRole
    family_id: uuid.UUID
    device_id: str  # 随 token 绑定;中间件比对 X-Device-Id 头免查 DB
    expires_at: Optional[datetime]
    # ISO date(北京时间)上一次滚动续期的日期;每日首次命中时触发 DB UPDATE
    last_rolled_date: Optional[str] = None


# Redis key 前缀,与其它域(`audit:` / `chat:` 等)隔开,避免 namespace 撞车
REDIS_KEY_PREFIX = "auth:"
# 600s = 10min。远小于 token 有效期(7d),resolve 时会续 TTL;
# 短 TTL 缩小泄露面 + 控制内存增长,长 TTL 降低 DB 压力
REDIS_TTL_SECONDS = 600

# 续期按日用北京时间:业务侧"今天是否续过"按家庭所在地自然日算,
# 避免子端跨时区往返出现同日两次续期 / 漏续。
# SHANGHAI 时区与 now_shanghai() 取自 app.core.time(单一来源)。


def _today_shanghai() -> str:
    """返回今天(北京时间)的 ISO date 字符串。续期判断的唯一时间源。"""
    return now_shanghai().date().isoformat()


def _redis_key(th: str) -> str:
    """拼 `auth:<token_hash>` Redis key,集中保证前缀一致。"""
    return f"{REDIS_KEY_PREFIX}{th}"


def token_hash(token: str) -> str:
    """对明文 token 做 sha256 hex digest。

    作为入 DB 的 `token_hash` 与 Redis key 的 hash 来源;选择 sha256 而非
    bcrypt / argon2 是因为 token 本身是 256bit 随机串(`secrets.token_urlsafe`),
    已经是密码学强度,DB 泄露也不可逆推明文,无需再加盐慢哈希。

    Args:
        token: 明文 token。

    Returns:
        str: sha256 hex digest。
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
    """签发新 token:DB 写 `auth_tokens` 行 + stage Redis `setex`,返回明文 token。

    契约:
    - 调用方在 `issue_token` 之前必须 `revoke_all_active_tokens`,以保证
      "一次一设备"语义。
    - 调用链末尾必须 `await commit_with_redis(db, redis)`,DB 行与 Redis 缓存
      才一并落盘。
    - 返回的明文 token 不会再次出现在系统里,丢失即需重新登录。

    Args:
        db: async DB session;仅 `add` + `flush`,不 commit。
        user_id: 新 token 所属用户 UUID。
        role: 用户角色枚举(冗余字段,方便 Redis 消费方零开销读取)。
        family_id: 所属家庭 UUID(同 role,冗余缓存)。
        device_id: 绑定设备 UUID;后续请求 X-Device-Id 与之比对。
        ttl_days: 有效期天数;`None` 表示永不过期(子端 token)。
        device_info: 客户端设备信息字典,可选。

    Returns:
        str: 明文 token(url-safe base64,32 字节随机)。
    """
    token = secrets.token_urlsafe(32)
    th = token_hash(token)
    expires_at = now_utc() + timedelta(days=ttl_days) if ttl_days is not None else None
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
        last_rolled_date=_today_shanghai() if expires_at is not None else None,
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
    """纯读路径解析 token;不做 DB UPDATE,续期由 `get_current_account` 触发。

    流程:
    1. Redis 命中 → 校验 `expires_at` 未过期 → 刷 TTL → 返回。
       刷 TTL 失败不属业务状态,下次 miss 自愈。
    2. Redis miss → 查 DB(`auth_tokens` join `users`,过滤已撤销)→ 重建
       `TokenPayload` → 直接 `setex` 回填 Redis(不经 staging,失败下次 miss 重试)
       → 返回。
    3. 任意一步查到或缓存到但已过期 → 返回 `None`(由 deps 抛 401)。

    Args:
        db: async DB session;仅用于 miss 时查询,不修改。
        redis: async Redis 客户端;用于 get / expire / setex。
        token: 明文 token。

    Returns:
        Optional[TokenPayload]: token 有效时返回 payload;不存在、已撤销或已过期
        返回 `None`。
    """
    th = token_hash(token)
    cached = await redis.get(_redis_key(th))
    if cached is not None:
        payload = TokenPayload.model_validate_json(cached)
        if payload.expires_at is not None and payload.expires_at < now_utc():
            return None
        # 读路径 cache 维护:刷 TTL;失败下次 miss 自愈,不属业务状态
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
    if tok.expires_at is not None and tok.expires_at < now_utc():
        return None

    # last_rolled_date 初始化为 (expires_at - 7d).date(),让外层 needs_roll
    # 能触发今天的首次续期(若今天尚未续)
    seed_date = (
        (tok.expires_at - timedelta(days=7)).astimezone(SHANGHAI).date().isoformat()
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
    # 读路径回填:不经 staging;失败下次 miss 重试
    await redis.setex(_redis_key(th), REDIS_TTL_SECONDS, payload.model_dump_json())
    return payload


def needs_roll(payload: TokenPayload) -> bool:
    """判断 parent token 是否需要滚动续期。

    语义:
    - `expires_at is None`(子端永久 token)→ 永不需要续。
    - `last_rolled_date == 今天(北京时间)` → 今天已续过,跳过。
    - 其余(parent 且今天首次命中) → 需要续。

    Args:
        payload: 由 `resolve_token` 返回的 token 上下文。

    Returns:
        bool: 是否需要滚动续期。
    """
    return payload.expires_at is not None and payload.last_rolled_date != _today_shanghai()


async def roll_token_expiry(
    db: AsyncSession,
    *,
    token_hash_hex: str,
    payload: TokenPayload,
) -> TokenPayload:
    """滚动续期:DB UPDATE `expires_at += 7d` + stage Redis 覆盖新 payload。

    契约:
    - 调用方(典型为 `get_current_account`)在 `needs_roll` 为真时调用。
    - 调用链末尾必须 `await commit_with_redis(db, redis)` 才落盘。
    - 返回新 payload(已含新 `expires_at` 与今天的 `last_rolled_date`),
      避免外层再 `model_copy`。

    Args:
        db: async DB session;执行 UPDATE,不 commit。
        token_hash_hex: 目标 token 的 sha256 hex(用于 UPDATE WHERE 条件);
            注意是 token_hash,非明文 token。
        payload: 当前 token 上下文,作为返回值基础。

    Returns:
        TokenPayload: 已更新 `expires_at` 与 `last_rolled_date` 的新 payload。
    """
    new_expires = now_utc() + timedelta(days=7)
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
            "last_rolled_date": _today_shanghai(),
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

    Args:
        db: async DB session;执行 UPDATE,不 commit。
        token: 明文 token。
    """
    th = token_hash(token)
    await db.execute(
        update(AuthToken)
        .where(
            AuthToken.token_hash == th,
            AuthToken.revoked_at.is_(None),
        )
        .values(revoked_at=now_utc())
    )
    stage_redis_op(db, RedisOp(kind="delete", key=_redis_key(th)))


async def revoke_all_active_tokens(db: AsyncSession, user_id: uuid.UUID) -> int:
    """撤销某用户所有未撤销的 token,返回被撤销的 token 数量。

    两步式:
    1. 先 SELECT 拿到所有 token_hash(用于后续批量 stage Redis delete)。
    2. 再 UPDATE 标 `revoked_at`,然后循环 stage Redis delete。
    不直接 SELECT-then-UPDATE 拼一条 SQL,是因为 UPDATE 返回的影响行
    不会带原 token_hash,Redis delete 需要 hash 才能拼 key。

    用途:登录成功后的"一次一设备"语义(配合 `issue_token` 使用)。

    Args:
        db: async DB session;执行 SELECT + UPDATE,不 commit。
        user_id: 目标用户 UUID。

    Returns:
        int: 被撤销的 token 数量(0 表示没有活跃 token)。
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
        .values(revoked_at=now_utc())
    )
    for th in hashes:
        stage_redis_op(db, RedisOp(kind="delete", key=_redis_key(th)))
    return len(hashes)
