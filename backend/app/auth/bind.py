"""child 绑定 token：纯 Redis 写（issue / peek / stage_consume）。"""
from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.redis_ops import RedisOp, stage_redis_op

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


async def peek_bind_token(
    redis: Redis, bind_token: str,
) -> Optional[tuple[uuid.UUID, uuid.UUID]]:
    """只 GET 不删。后续 DB 写入 + stage_consume_bind_token + commit_with_redis
    完成后才真正把 bind_token 从 Redis 删掉，DB 回滚则自动放弃删除（bind_token
    还在 5min TTL 内可重试）。"""
    raw = await redis.get(f"{BIND_KEY_PREFIX}{bind_token}")
    if raw is None:
        return None
    data = json.loads(raw)
    return uuid.UUID(data["parent_user_id"]), uuid.UUID(data["child_user_id"])


def stage_consume_bind_token(db: AsyncSession, bind_token: str) -> None:
    stage_redis_op(db, RedisOp(kind="delete", key=f"{BIND_KEY_PREFIX}{bind_token}"))


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
