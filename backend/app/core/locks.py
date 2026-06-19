"""通用 Redis 锁原语。

提供两类锁：
- `acquire_throttle_lock`：父端 chat 流节流用的 1.5 秒短期锁；
- `acquire_session_lock` / `release_session_lock`：session 级 180 秒互斥锁，
  用 Lua 脚本做 compare-and-delete 释放。

Redis key 命名空间与 auth / bind / audit 域隔开，也供 `app/api/me.py`
反查锁状态（`api/*` import `core/*` 合法，符合 D-1 边界）。

进程级 stop event 登记表的契约在 `app/domain/chat/stream_signals` 中。
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

# Redis key 命名空间：与 auth/bind/audit 域隔开，也供 `app/api/me.py` 反查锁状态。
CHAT_THROTTLE_KEY_PREFIX = "chat:throttle:"
CHAT_LOCK_KEY_PREFIX = "chat:lock:"

RELEASE_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
else
  return 0
end
"""
"""原子 compare-and-delete Lua 脚本。`KEYS[1]` 为锁 key，`ARGV[1]` 为 expected nonce。"""


async def acquire_throttle_lock(redis: "Redis", child_user_id: str) -> bool:
    """为子端用户获取 1.5 秒节流锁。

    用 `SETNX` + 1.5s TTL 语义：同一 `child_user_id` 在 TTL 窗口内的快速重试
    会被直接拒绝（返回 `False`）。

    Args:
        redis: 业务主 Redis（db=0）。
        child_user_id: 子端用户标识。

    Returns:
        成功获取返回 `True`，TTL 窗口内重复获取返回 `False`。
    """
    key = f"{CHAT_THROTTLE_KEY_PREFIX}{child_user_id}"
    return await redis.set(key, "1", nx=True, px=1500)


async def acquire_session_lock(redis: "Redis", session_id: str) -> str | None:
    """为指定 session 获取 180 秒互斥锁。

    返回 32 字符十六进制 nonce；调用方必须在 `release_session_lock` 中
    传入相同 nonce 才能原子地删除 key（Lua compare-and-delete）。
    当 session 已被锁时返回 `None`。

    Args:
        redis: 业务主 Redis（db=0）。
        session_id: session 标识。

    Returns:
        成功时返回 nonce 字符串，已被锁时返回 `None`。
    """
    key = f"{CHAT_LOCK_KEY_PREFIX}{session_id}"
    nonce = secrets.token_hex(16)
    ok = await redis.set(key, nonce, nx=True, px=180_000)
    return nonce if ok else None


async def release_session_lock(redis: "Redis", session_id: str, nonce: str) -> None:
    """仅在 nonce 匹配时原子释放 session 锁。

    通过内联 Lua 脚本把 `get + delete` 合为一次原子操作，避免在
    `acquire` 与 `release` 之间锁被其他请求续期后被误删。

    Args:
        redis: 业务主 Redis（db=0）。
        session_id: session 标识。
        nonce: 由 `acquire_session_lock` 返回的 nonce。
    """
    await redis.eval(
        RELEASE_LOCK_LUA,
        1,
        f"{CHAT_LOCK_KEY_PREFIX}{session_id}",
        nonce,
    )  # type: ignore[reportGeneralTypeIssues]  # type stub bug: Awaitable[str]|str, real impl always awaitable
