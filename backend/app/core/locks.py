"""Redis 锁原语 — 父端 chat 流节流 + session 级互斥。

部署约束(M6):单 uvicorn worker (--workers 1)。Redis 锁假设同进程触发
acquire / release 配对,跨进程互斥由 sticky session routing first 兜底,
进一步升级走 Redis Pub/Sub fallback。See baseline §3.3.

进程级 stop event 登记表见 `app.domain.chat.stream_signals`(拆 D-1 边界:
锁契约归此处,stop signal 登记契约归 stream_signals)。
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

RELEASE_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
else
  return 0
end
"""
"""Atomic compare-and-delete Lua script. KEYS[1]=lock key; ARGV[1]=expected nonce."""


async def acquire_throttle_lock(redis: "Redis", child_user_id: str) -> bool:
    """Acquire a 1.5-second throttle lock for a child user.

    Uses SETNX + 1.5s TTL so that rapid re-stream attempts within
    the TTL window are rejected (return False).
    """
    key = f"chat:throttle:{child_user_id}"
    return await redis.set(key, "1", nx=True, px=1500)


async def acquire_session_lock(redis: "Redis", session_id: str) -> str | None:
    """Acquire a 180-second session-level lock.

    Returns a 32-char hex nonce on success; caller must pass the same
    nonce to release_session_lock to atomically delete the key (Lua
    compare-and-delete). Returns None if the session is already locked.
    """
    key = f"chat:lock:{session_id}"
    nonce = secrets.token_hex(16)
    ok = await redis.set(key, nonce, nx=True, px=180_000)
    return nonce if ok else None


async def release_session_lock(redis: "Redis", session_id: str, nonce: str) -> None:
    """Release a session lock only if the supplied nonce matches.

    Uses an inline Lua script so the get+delete is atomic and safe
    against accidental deletion of a lock that was renewed by another
    request between acquire and release.
    """
    await redis.eval(
        RELEASE_LOCK_LUA,
        1,
        f"chat:lock:{session_id}",
        nonce,
    )  # type: ignore[reportGeneralTypeIssues]  # type stub bug: Awaitable[str]|str, real impl always awaitable
