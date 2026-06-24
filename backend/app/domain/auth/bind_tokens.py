"""子账号绑定 token 生命周期:issue / consume / stage。

提供 `issue_bind_token` / `consume_bind_token` / `stage_record_bind_result`。

设计:bind_token 是 5 分钟一次性凭证,纯 Redis 存储(DB 不持久化);consume 走
Redis `GETDEL` 原子"读 + 删",并发 redeem 第二个必拿到 `None`,杜绝双发 token。
DB 写入失败时 bind_token 不再可重试——这是有意为之,与 CLAUDE.md「DB 是 source of truth」
一致(若 DB 无 `auth_tokens` 记录,客户端即使拿到 bind_token 也不该再有重试机会)。

另维护一组 `bind_result:` key:`stage_record_bind_result` 在 redeem 成功路径里 stage,
供父端轮询 `/bind-tokens/{bind_token}/status` 端点查询子端扫码结果。
"""

from __future__ import annotations

import json
import secrets
import uuid
from typing import Optional

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import RedisOp, stage_redis_op
from app.core.time import now_utc

BIND_KEY_PREFIX = "bind:"
BIND_TTL_SECONDS = 300
BIND_RESULT_KEY_PREFIX = "bind_result:"
BIND_RESULT_TTL_SECONDS = 600


async def issue_bind_token(
    redis: Redis,
    *,
    parent_user_id: uuid.UUID,
    child_user_id: uuid.UUID,
) -> str:
    """签发一次性 bind_token。

    纯 Redis 写(无 DB 变更需共同 commit),直接 `setex`,不经 staging。
    key 前缀 `BIND_KEY_PREFIX`,TTL `BIND_TTL_SECONDS`;value 为 parent 与
    child 的 UUID 字符串对,供 redeem 端点反查家庭归属。

    Args:
        redis: async Redis 客户端。
        parent_user_id: 调用方 parent 的 user UUID。
        child_user_id: 目标 child 的 user UUID。

    Returns:
        str: bind_token 明文(url-safe base64,16 字节随机)。
    """
    token = secrets.token_urlsafe(16)
    await redis.setex(
        f"{BIND_KEY_PREFIX}{token}",
        BIND_TTL_SECONDS,
        json.dumps(
            {
                "parent_user_id": str(parent_user_id),
                "child_user_id": str(child_user_id),
            }
        ),
    )
    return token


async def consume_bind_token(
    redis: Redis,
    bind_token: str,
) -> Optional[tuple[uuid.UUID, uuid.UUID]]:
    """原子地读取并删除 bind_token。

    走 Redis `GETDEL`,并发 redeem 第二个必拿到 `None`,杜绝双发 token。
    bind_token 一旦被 consume 即不可重试(无论后续 DB 写入成败)。

    Args:
        redis: async Redis 客户端。
        bind_token: 明文 bind_token。

    Returns:
        Optional[tuple[uuid.UUID, uuid.UUID]]: 成功时返回 `(parent_user_id, child_user_id)`;
        bind_token 不存在或已过期返回 `None`。
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
    """Stage「bind_token 已成功兑换」结果,供父端轮询端点读取。

    写入 key 前缀 `BIND_RESULT_KEY_PREFIX`,TTL `BIND_RESULT_TTL_SECONDS`。
    通过 `stage_redis_op` 挂到 `db.info[pending_redis_ops]`,与 DB 写入在
    `commit_with_redis` 里原子地决定落不落——DB 回滚则此条也不 flush,父端
    继续看到 `pending`。

    Args:
        db: async DB session;仅 stage,不 commit。
        bind_token: 已成功兑换的 bind_token 明文。
        child_user_id: 已完成兑换的 child UUID。
    """
    stage_redis_op(
        db,
        RedisOp(
            kind="setex",
            key=f"{BIND_RESULT_KEY_PREFIX}{bind_token}",
            ttl_seconds=BIND_RESULT_TTL_SECONDS,
            value=json.dumps(
                {
                    "child_user_id": str(child_user_id),
                    "bound_at": now_utc().isoformat(),
                }
            ),
        ),
    )
