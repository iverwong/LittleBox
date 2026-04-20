"""DB↔Redis 同步的统一封装。业务代码不直接写 Redis，改为 stage 到 session.info，
由 commit_with_redis 先 DB commit 再 flush Redis；策略在此一点统一。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
_PENDING_KEY = "pending_redis_ops"


@dataclass
class RedisOp:
    kind: Literal["setex", "delete"]
    key: str
    ttl_seconds: int = 0
    value: str | None = None


def stage_redis_op(db: AsyncSession, op: RedisOp) -> None:
    """挂一条 Redis 操作到 session.info，由 commit_with_redis 统一 flush。
    session close / rollback 时自然丢弃。"""
    db.info.setdefault(_PENDING_KEY, []).append(op)


def discard_pending_redis_ops(db: AsyncSession) -> None:
    """显式丢弃（决定不 commit 但也不想触发 teardown 护栏时用）。"""
    db.info.pop(_PENDING_KEY, None)


async def commit_with_redis(db: AsyncSession, redis: Redis) -> None:
    """业务唯一推荐的 commit 入口：先 DB commit，再 flush 挂载的 Redis ops。

    - DB commit 报错 → ops 已 pop，直接丢弃；异常上抛；
    - DB commit 成功但 Redis flush 报错 → log error 不抛；业务语义由 DB 决定，
      Redis 是缓存允许临时不一致，下次 miss 回填或 TTL 到期自愈。
    """
    ops: list[RedisOp] = db.info.pop(_PENDING_KEY, [])
    await db.commit()
    if not ops:
        return
    try:
        async with redis.pipeline(transaction=False) as pipe:
            for op in ops:
                if op.kind == "setex":
                    assert op.value is not None
                    pipe.setex(op.key, op.ttl_seconds, op.value)
                elif op.kind == "delete":
                    pipe.delete(op.key)
            await pipe.execute()
    except Exception:
        logger.exception("redis flush failed after db commit; cache self-heals via TTL")
