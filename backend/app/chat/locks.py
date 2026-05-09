"""Locks and stop-event registry for the main dialogue stream.

Deployment contract (M6): single uvicorn worker (--workers 1).
running_streams is an in-process dict; cross-process stop signaling
is NOT implemented. When capacity monitors trigger (event loop lag
> 50ms / process memory > 3.5G / concurrent streams > 200), upgrade
path is: sticky session routing first, Redis Pub/Sub fallback.
See baseline §3.3.

Cleanup contract: running_streams entries are removed by the caller
in a finally block via `pop(sid, None)` after the stream ends.
Step 2 does not test cleanup; cleanup correctness is covered by
Step 8c integration tests.
"""
import asyncio
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

running_streams: dict[str, asyncio.Event] = {}
"""Module-level registry of active stream stop events. key=session_id, value=asyncio.Event."""

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


async def release_session_lock(
    redis: "Redis", session_id: str, nonce: str
) -> None:
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
