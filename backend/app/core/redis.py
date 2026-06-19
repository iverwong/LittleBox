"""Redis 客户端层 + DB↔Redis 同步纪律。

模块内分两段职责：
1. 客户端层：Redis 连接工厂 + FastAPI lifespan 钩子 + Depends 注入
   - `_build_arq_redis_url`：派生 ARQ 专用 db URL（被 `core/runtime.py` 复用）；
   - `redis_lifespan`：挂到 FastAPI lifespan；
   - `_redis`：模块私有变量，业务走 `Depends(get_redis)`；
   - `get_redis`：主业务 Redis（db=0）。
2. 同步层：DB commit 与 Redis flush 顺序保证
   - `RedisOp` dataclass（`setex` / `delete`）；
   - `stage_redis_op`：挂到 `session.info`，随 `commit_with_redis` 统一 flush；
   - `discard_pending_redis_ops`：显式丢弃挂载的 ops；
   - `commit_with_redis`：先 DB commit，再 flush Redis ops；DB 失败则 ops
     丢弃；Redis 失败 log 不抛。

业务统一通过 `RuntimeResources.audit_redis` 取审查专用 Redis（由
`build_runtime` 创建），不再单独提供 `get_audit_redis` 入口。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse, urlunparse

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

# ---- 客户端层 ----

_redis: Redis | None = None
"""模块级主 Redis（db=0）句柄，由 `redis_lifespan` 在 startup 时创建。"""


def _build_arq_redis_url() -> str:
    """从 `settings.redis_url` 派生 ARQ 专用 db URL（`db=arq_redis_db`）。

    单一来源：`core/runtime.py::build_runtime` 也调用本函数派生
    `audit_redis` URL。

    Returns:
        替换 path 为 `arq_redis_db` 后的完整 Redis URL。
    """
    parsed = urlparse(settings.redis_url)
    return urlunparse(parsed._replace(path=f"/{settings.arq_redis_db}"))


@asynccontextmanager
async def redis_lifespan() -> AsyncGenerator[None, None]:
    """FastAPI lifespan 钩子：创建主 Redis 连接，退出时关闭。

    启动时用 `settings.redis_url` 创建 db=0 的主 Redis；关闭时调用
    `aclose()` 释放连接，并把模块句柄置回 `None`。
    """
    global _redis
    _redis = Redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )
    try:
        yield
    finally:
        await _redis.aclose()
        _redis = None


async def get_redis() -> Redis:
    """FastAPI 依赖：返回主业务 Redis（db=0）。

    Returns:
        lifespan 阶段创建的 `Redis` 实例。

    Raises:
        AssertionError: lifespan 未正确初始化时触发。
    """
    assert _redis is not None, "redis pool not initialized (check lifespan)"
    return _redis


# ---- 同步层 ----

logger = logging.getLogger(__name__)
_PENDING_KEY = "pending_redis_ops"
"""挂在 `AsyncSession.info` 上的 list key，保存待 flush 的 Redis 操作。"""


@dataclass
class RedisOp:
    """待 flush 的 Redis 操作描述。

    Attributes:
        kind: 操作类型，取值 `"setex"` 或 `"delete"`。
        key: Redis key。
        ttl_seconds: 仅 `setex` 有意义，TTL 秒数。
        value: 仅 `setex` 有意义，待写入的字符串值。
    """

    kind: Literal["setex", "delete"]
    key: str
    ttl_seconds: int = 0
    value: str | None = None


def stage_redis_op(db: AsyncSession, op: RedisOp) -> None:
    """挂一条 Redis 操作到 `session.info`，由 `commit_with_redis` 统一 flush。

    session 被 rollback / close 时挂载的 ops 自然丢弃。

    Args:
        db: 业务 `AsyncSession`。
        op: 待执行的 Redis 操作。
    """
    db.info.setdefault(_PENDING_KEY, []).append(op)


def discard_pending_redis_ops(db: AsyncSession) -> None:
    """显式丢弃当前 session 挂载的全部 Redis ops。

    用于「决定不 commit、但也不希望触发 teardown 护栏」的边界场景。

    Args:
        db: 业务 `AsyncSession`。
    """
    db.info.pop(_PENDING_KEY, None)


async def commit_with_redis(db: AsyncSession, redis: Redis) -> None:
    """业务唯一推荐的 commit 入口：先 DB commit，再 flush 挂载的 Redis ops。

    同步纪律：
    - DB commit 报错 → ops 已被 pop，直接丢弃；异常上抛；
    - DB commit 成功、Redis flush 报错 → log error 不抛；业务语义由 DB 决定，
      Redis 是缓存层，允许临时不一致，下次 miss 回填或 TTL 到期自愈。

    Args:
        db: 业务 `AsyncSession`。
        redis: 用于执行 pipeline 的 Redis 连接（通常为主业务 Redis）。
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
