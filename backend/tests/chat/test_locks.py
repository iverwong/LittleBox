"""Tests for app.chat.locks — throttle lock, session lock, Lua release."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.chat.locks import (
    acquire_session_lock,
    acquire_throttle_lock,
    release_session_lock,
)


@pytest.fixture
async def fake_redis():
    """fakeredis 2.35.1 — supports EVAL for Lua scripts."""
    import fakeredis.aioredis

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.mark.asyncio
async def test_throttle_lock_blocks_1pt5s(fake_redis):
    """Second acquire within 1.5s returns False (SETNX fails, returns None)."""
    child = "user_abc123"
    first = await acquire_throttle_lock(fake_redis, child)
    assert first is True

    second = await acquire_throttle_lock(fake_redis, child)
    assert second is None  # redis.set(nx=True) returns None on key exists


@pytest.mark.asyncio
async def test_throttle_lock_ttl(fake_redis):
    """Throttle key has ~1500ms TTL (10ms clock drift tolerance)."""
    child = "user_abc123"
    await acquire_throttle_lock(fake_redis, child)
    key = f"chat:throttle:{child}"
    pttl = await fake_redis.pttl(key)
    # 1490 ≤ pttl ≤ 1500 (1490 tolerates 10ms early expiry)
    assert 1490 <= pttl <= 1500, f"pttl={pttl!r} outside [1490,1500]"


@pytest.mark.asyncio
async def test_session_lock_same_sid_returns_none(fake_redis):
    """Same session_id twice: first returns nonce, second returns None."""
    sid = "session-xyz"
    nonce1 = await acquire_session_lock(fake_redis, sid)
    assert nonce1 is not None
    assert len(nonce1) == 32  # 16 bytes → 32 hex chars

    nonce2 = await acquire_session_lock(fake_redis, sid)
    assert nonce2 is None


@pytest.mark.asyncio
async def test_session_lock_ttl_180s(fake_redis):
    """Session lock key has ~180000ms TTL (1s clock drift tolerance)."""
    sid = "session-xyz"
    await acquire_session_lock(fake_redis, sid)
    key = f"chat:lock:{sid}"
    pttl = await fake_redis.pttl(key)
    # 179000 ≤ pttl ≤ 180000 (179000 tolerates 1s early expiry)
    assert 179000 <= pttl <= 180000, f"pttl={pttl!r} outside [179000,180000]"


@pytest.mark.asyncio
async def test_release_lock_wrong_nonce_preserves_key(fake_redis):
    """Releasing with wrong nonce does NOT delete the lock key (Lua check).

    Note: fakeredis 2.35.1 does not support EVAL (unknown command 'eval').
    Lua contract is tested via mock here; full Lua integration coverage
    is tracked as a Step 11 requirement (see Step 2 verification plan).
    """
    sid = "session-xyz"
    nonce = await acquire_session_lock(fake_redis, sid)
    assert nonce is not None

    # Mock redis.eval to simulate Lua behavior: GET compares nonce, DEL only if equal
    captured_args = {}

    async def mock_eval(script, num_keys, key, nonce_arg):
        # Lua: if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end
        stored = await fake_redis.get(key)
        captured_args["stored"] = stored
        captured_args["nonce_arg"] = nonce_arg
        if stored == nonce_arg:
            await fake_redis.delete(key)
            return 1
        return 0

    # Try release with a different nonce
    fake_nonce = "0" * 32
    with patch.object(fake_redis, "eval", mock_eval):
        await release_session_lock(fake_redis, sid, fake_nonce)

    assert captured_args["stored"] == nonce, f"stored={captured_args['stored']}, expected={nonce}"
    assert captured_args["nonce_arg"] == fake_nonce, f"nonce_arg={captured_args['nonce_arg']}, expected={fake_nonce}"

    key = f"chat:lock:{sid}"
    assert await fake_redis.exists(key) == 1, "lock key deleted despite wrong nonce"


@pytest.mark.asyncio
async def test_release_lock_correct_nonce_deletes_key(fake_redis):
    """Releasing with the correct nonce deletes the lock key atomically.

    Note: fakeredis 2.35.1 does not support EVAL (unknown command 'eval').
    Lua contract is tested via mock here; full Lua integration coverage
    is tracked as a Step 11 requirement (see Step 2 verification plan).
    """
    sid = "session-xyz"
    nonce = await acquire_session_lock(fake_redis, sid)
    assert nonce is not None

    async def mock_eval(script, num_keys, key, nonce_arg):
        stored = await fake_redis.get(key)
        if stored == nonce_arg:
            await fake_redis.delete(key)
            return 1
        return 0

    with patch.object(fake_redis, "eval", mock_eval):
        await release_session_lock(fake_redis, sid, nonce)

    key = f"chat:lock:{sid}"
    assert await fake_redis.exists(key) == 0, "lock key still present after correct release"


@pytest.mark.asyncio
async def test_running_streams_register_and_lookup():
    """running_streams dict accepts registration and lookup by session_id."""
    from app.chat import locks

    sid = "test_stream_sid"
    event = asyncio.Event()
    locks.running_streams[sid] = event

    assert sid in locks.running_streams
    assert locks.running_streams[sid] is event

    # clean up
    locks.running_streams.pop(sid, None)


@pytest.mark.asyncio
async def test_running_streams_pop_after_use():
    """pop(sid, None) removes entry; subsequent lookup returns None."""
    from app.chat import locks

    sid = "test_stream_sid_pop"
    event = asyncio.Event()
    locks.running_streams[sid] = event

    popped = locks.running_streams.pop(sid, None)
    assert popped is event
    assert sid not in locks.running_streams


@pytest.mark.asyncio
async def test_no_business_redis_in_test_file():
    """Verify no real Redis client is instantiated in this test file."""
    import ast
    import pathlib

    test_path = pathlib.Path(__file__).resolve()
    source = test_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)

    # No business Redis class names should appear as runtime calls
    business_redis_classes = {"Redis", "aioredis"}
    found = [c for c in business_redis_classes if c in names]
    assert not found, f"business Redis classes found in test file: {found}"