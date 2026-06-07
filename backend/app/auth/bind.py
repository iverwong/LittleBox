"""child 绑定 token：纯 Redis 写（issue / consume / record_result）。

设计：bind_token 是 5min 一次性凭证。consume 用 Redis GETDEL 原子「读+删」,
并发 redeem 第二个必拿 None，杜绝双发 token。DB 写入失败时 bind_token 不再可
重试——这是有意为之,与 CLAUDE.md「DB 是 source of truth」一致(若 DB 无
auth_token 记录,客户端即使拿到 bind_token 也不该再有重试机会)。
"""
from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import RedisOp, stage_redis_op

BIND_KEY_PREFIX = "bind:"
BIND_TTL_SECONDS = 300
BIND_RESULT_KEY_PREFIX = "bind_result:"
BIND_RESULT_TTL_SECONDS = 600


async def issue_bind_token(
    redis: Redis, *, parent_user_id: uuid.UUID, child_user_id: uuid.UUID,
) -> str:
    """签一次性 bind_token。纯 Redis 写（无 DB 变更需共同 commit），
    直接 setex，不进 staging。"""
    token = secrets.token_urlsafe(16)
    await redis.setex(
        f"{BIND_KEY_PREFIX}{token}",
        BIND_TTL_SECONDS,
        json.dumps({
            "parent_user_id": str(parent_user_id),
            "child_user_id": str(child_user_id),
        }),
    )
    return token


async def consume_bind_token(
    redis: Redis, bind_token: str,
) -> Optional[tuple[uuid.UUID, uuid.UUID]]:
    """GETDEL：原子地「读+删」,并发 redeem 第二个必拿 None。

    返回 (parent_user_id, child_user_id) 供调用方做 family 边界/角色等校验。
    bind_token 一旦被 consume 即不可重试(无论后续 DB 写入成败)。
    """
    raw = await redis.getdel(f"{BIND_KEY_PREFIX}{bind_token}")
    if raw is None:
        return None
    data = json.loads(raw)
    return uuid.UUID(data["parent_user_id"]), uuid.UUID(data["child_user_id"])


def stage_record_bind_result(
    db: AsyncSession,
    bind_token: str,
    child_user_id: uuid.UUID,
) -> None:
    """把「bind_token 已成功兑换」的事实 stage 到 session.info，由 commit_with_redis
    和 DB 写入原子地决定落不落——DB 回滚则此条也不 flush，父端继续看到 pending。"""
    stage_redis_op(db, RedisOp(
        kind="setex",
        key=f"{BIND_RESULT_KEY_PREFIX}{bind_token}",
        ttl_seconds=BIND_RESULT_TTL_SECONDS,
        value=json.dumps({
            "child_user_id": str(child_user_id),
            "bound_at": datetime.now(timezone.utc).isoformat(),
        }),
    ))
