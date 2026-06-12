"""Redis 客户端层 + DB↔Redis 同步纪律(D-4,Phase 4.4)。

D-4 决议:把分散在 auth/redis_client.py + auth/redis_ops.py 的两段职责
合并到本文件,模块 docstring 标明两段边界。

两段职责:
  1. 客户端层:Redis 连接工厂 + FastAPI lifespan 钩子 + Depends 注入
     - _build_arq_redis_url(派生 ARQ 专用 db URL,被 core/runtime.py 复用)
     - redis_lifespan(挂到 FastAPI lifespan)
     - _redis / _audit_redis(模块私有变量,业务走 rr.audit_redis / Depends(get_redis))
     - get_redis(主业务 Redis db=0)

  2. 同步层:DB commit 与 Redis flush 顺序保证
     - RedisOp dataclass(setex / delete)
     - stage_redis_op(挂到 session.info,commit_with_redis 统一 flush)
     - discard_pending_redis_ops(显式丢弃)
     - commit_with_redis(先 DB commit,再 flush Redis ops;DB 失败 ops 丢弃,Redis 失败 log 不抛)

D-4A.1 决议:不创建 get_audit_redis。当前代码无此函数,业务统一走
rr.audit_redis(RuntimeResources 注入);_audit_redis 是 redis_lifespan
内部模块私有变量,Phase 6 之前保留(疑似死代码,Phase 6 收口)。
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
_audit_redis: Redis | None = None


def _build_arq_redis_url() -> str:
    """从 settings.redis_url 派生 ARQ 专用 db URL(db=arq_redis_db)。

    单一来源:core/runtime.py::build_runtime 也调本函数派生 audit_redis URL。
    """
    parsed = urlparse(settings.redis_url)
    return urlunparse(parsed._replace(path=f"/{settings.arq_redis_db}"))


@asynccontextmanager
async def redis_lifespan() -> AsyncGenerator[None, None]:
    """挂到 FastAPI lifespan:创建主 Redis + ARQ Redis 连接池。"""
    global _redis, _audit_redis
    _redis = Redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )
    _audit_redis = Redis.from_url(
        _build_arq_redis_url(),
        encoding="utf-8",
        decode_responses=True,
    )
    try:
        yield
    finally:
        await _redis.aclose()
        await _audit_redis.aclose()
        _redis = _audit_redis = None


async def get_redis() -> Redis:
    """主业务 Redis(db=0)。"""
    assert _redis is not None, "redis pool not initialized (check lifespan)"
    return _redis


# ---- 同步层 ----

logger = logging.getLogger(__name__)
_PENDING_KEY = "pending_redis_ops"


@dataclass
class RedisOp:
    kind: Literal["setex", "delete"]
    key: str
    ttl_seconds: int = 0
    value: str | None = None


def stage_redis_op(db: AsyncSession, op: RedisOp) -> None:
    """挂一条 Redis 操作到 session.info,由 commit_with_redis 统一 flush。
    session close / rollback 时自然丢弃。"""
    db.info.setdefault(_PENDING_KEY, []).append(op)


def discard_pending_redis_ops(db: AsyncSession) -> None:
    """显式丢弃(决定不 commit 但也不想触发 teardown 护栏时用)。"""
    db.info.pop(_PENDING_KEY, None)


async def commit_with_redis(db: AsyncSession, redis: Redis) -> None:
    """业务唯一推荐的 commit 入口:先 DB commit,再 flush 挂载的 Redis ops。

    - DB commit 报错 → ops 已 pop,直接丢弃;异常上抛;
    - DB commit 成功但 Redis flush 报错 → log error 不抛;业务语义由 DB 决定,
      Redis 是缓存允许临时不一致,下次 miss 回填或 TTL 到期自愈。
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
