"""Tests for POST /me/chat/stream stop detection + 不 cancel (Step 8c).

Verifies:
- StopNoAi: event set before content → stopped without aid, DB human only
- StopWithAi: event set after content → stopped with aid, DB ai + 'user_stopped'
- KeepGo (不 cancel): ConnectionError at yield → LLM stream continues, all
  chunks consumed, DB written, lock released
- running_streams cleanup in generator finally block
- Lock release via Lua nonce DEL (key gone, re-acquirable)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from unittest.mock import patch

import anyio
import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.auth.redis_ops import commit_with_redis
from app.auth.tokens import issue_token
from app.chat.graph import main_graph
from app.chat.locks import running_streams
from app.db import get_db
from app.models.accounts import Family, FamilyMember, User
from app.models.chat import Message
from app.models.enums import MessageRole, MessageStatus, UserRole

# ---------------------------------------------------------------------------
# _BrokenOnCall: 通过 athrow 将异常注入生成器的 yield 暂停点，
# 触发 app/api/me.py 中内层 ``except (ConnectionError, anyio.BrokenResourceError,
# asyncio.CancelledError)`` 路径（Step 8c advisory #1）。
# httpx ASGI transport 无法直接从外部触发 yield 点异常，因为
# GeneratorExit / 普通 __anext__ raise 都走外层 except。
# ---------------------------------------------------------------------------


class _BrokenOnCall:
    """Wrap an async iterator; on the Nth ``__anext__`` call use ``athrow()``
    to inject ``anyio.BrokenResourceError`` into the generator at its current
    yield point, exercising the inner try/except at ``yield frame``.

    Args:
        iterator: The real async iterator (the generator from StreamingResponse).
        break_after: After this many successful ``__anext__`` calls, the next
                     call injects the error.  Default 2 = inject on call 3
                     (after session_meta + first delta have been yielded).
    """

    def __init__(self, iterator: AsyncIterator, break_after: int = 2) -> None:
        self._it = iterator
        self._break_after = break_after
        self._count = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        self._count += 1
        if self._count > self._break_after:
            # Inject BrokenResourceError INTO the generator at yield point.
            # The inner ``except anyio.BrokenResourceError`` in me.py catches it,
            # sets client_alive = False, and continues consuming the graph stream
            # (不 cancel 语义).
            try:
                return await self._it.athrow(anyio.BrokenResourceError("Broken pipe"))
            except StopAsyncIteration:
                raise
        try:
            return await self._it.__anext__()
        except StopAsyncIteration:
            raise

    async def asend(self, *args):
        return await self._it.asend(*args)

    async def athrow(self, *args):
        return await self._it.athrow(*args)

    async def aclose(self):
        await self._it.aclose()


# ---------------------------------------------------------------------------
# Fixtures (same pattern as test_chat_stream_graph.py)
# ---------------------------------------------------------------------------


@pytest.fixture
async def redis_client_with_eval(redis_client: FakeRedis) -> FakeRedis:
    """Patch FakeRedis.eval to simulate Lua DEL-if-nonce-match."""
    import fakeredis.aioredis

    async def mock_eval(self, script: str, num_keys: int, key: str, nonce_arg: str) -> int:  # noqa: N805
        stored = await self.get(key)
        if stored == nonce_arg:
            await self.delete(key)
            return 1
        return 0

    original_eval = fakeredis.aioredis.FakeRedis.eval
    fakeredis.aioredis.FakeRedis.eval = mock_eval
    yield redis_client
    fakeredis.aioredis.FakeRedis.eval = original_eval


@pytest.fixture
async def app_with_eval(db_session, redis_client_with_eval):
    """App fixture with patched redis for Lua DEL simulation."""
    from app.auth.redis_client import get_redis
    from app.main import create_app

    application = create_app()

    async def _get_db():
        yield db_session

    async def _get_redis():
        return redis_client_with_eval

    application.dependency_overrides[get_db] = _get_db
    application.dependency_overrides[get_redis] = _get_redis
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
async def api_client_with_eval(app_with_eval):
    """Async client bound to app_with_eval."""
    transport = ASGITransport(app=app_with_eval)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
async def child_user(db_session):
    """Child + family (no ChildProfile)."""
    fam = Family()
    db_session.add(fam)
    await db_session.flush()

    user = User(
        family_id=fam.id,
        role=UserRole.child,
        phone="0000",
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    db_session.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.child))
    await db_session.commit()
    return user


@pytest.fixture
async def auth_headers_child(db_session, redis_client_with_eval, child_user):
    """Return (headers, child_user) with a valid child token + device-id."""
    device_id = "test-device-8c"
    token = await issue_token(
        db_session,
        user_id=child_user.id,
        role=UserRole.child,
        family_id=child_user.family_id,
        device_id=device_id,
        ttl_days=None,
    )
    await commit_with_redis(db_session, redis_client_with_eval)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Device-Id": device_id,
    }
    return headers, child_user


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_payload(
    content: str = "hello",
    session_id: str | None = None,
    regenerate_for: str | None = None,
) -> dict:
    body: dict = {"content": content}
    if session_id is not None:
        body["session_id"] = session_id
    if regenerate_for is not None:
        body["regenerate_for"] = regenerate_for
    return body


def _parse_sse_frames(raw: str) -> list[dict]:
    """Parse SSE multi-line stream into list of {type, data} dicts."""
    events: list[dict] = []
    current_type = None
    for line in raw.split("\n"):
        if line.startswith("event:"):
            current_type = line[len("event:"):].strip()
        elif line.startswith("data:") and current_type is not None:
            data_str = line[len("data:"):].strip()
            events.append({"type": current_type, "data": json.loads(data_str)})
    return events


def _make_delta_frame(text: str) -> bytes:
    """Build an SSE delta frame bytes."""
    d = json.dumps({"content": text}, ensure_ascii=False)
    return f"event: delta\ndata: {d}\n\n".encode()


# ---------------------------------------------------------------------------
# S1: StopNoAi — event set before any content emitted
# ---------------------------------------------------------------------------
#
# Key design: fake stream looks up the asyncio.Event from running_streams
# (which the generator registered between session_meta yield and the for loop),
# sets it, then yields a finish_reason payload.  The generator's stop check
# fires on the first (and only) iteration → StopNoAi.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_no_ai(
    api_client_with_eval, auth_headers_child, db_session, redis_client,
):
    """StopNoAi: event set before graph yields content → stopped without aid, DB human only."""
    headers, child = auth_headers_child

    async def fake_astream_no_content(initial_state, stream_mode="custom"):
        # Generator registered its event in running_streams before calling astream;
        # look it up and set it before yielding any content-bearing payload.
        sid = initial_state["session_id"]
        ev = running_streams.get(sid)
        if ev is not None:
            ev.set()
        yield {"finish_reason": "stop"}  # no delta → has_emitted_content stays False

    with patch.object(main_graph, "astream", fake_astream_no_content):
        body = make_payload(content="Hello")
        resp = await api_client_with_eval.post(
            "/api/v1/me/chat/stream", json=body, headers=headers,
        )
        assert resp.status_code == 200
        await resp.aclose()

    frames = _parse_sse_frames(resp.text)
    frame_types = [f["type"] for f in frames]

    assert "session_meta" in frame_types
    assert "stopped" in frame_types, f"No stopped frame in {frame_types}"
    assert "end" not in frame_types, "Unexpected end frame in StopNoAi path"
    assert "delta" not in frame_types, "Unexpected delta in StopNoAi path"

    stopped_frame = next(f for f in frames if f["type"] == "stopped")
    assert stopped_frame["data"]["finish_reason"] == "user_stopped"
    # StopNoAi: no aid in stopped frame
    assert "aid" not in stopped_frame["data"] or stopped_frame["data"]["aid"] is None

    sid = frames[0]["data"]["session_id"]

    # DB: only human row (from decision matrix), no ai row
    msgs = (
        (await db_session.execute(
            select(Message).where(Message.session_id == sid).order_by(Message.created_at),
        ))
        .scalars()
        .all()
    )
    assert len(msgs) == 1, f"Expected 1 human row, got {len(msgs)}"
    assert msgs[0].role == MessageRole.human
    assert msgs[0].status == MessageStatus.active

    # running_streams cleanup
    assert sid not in running_streams, "running_streams entry was not cleaned up"

    # Lock released
    lock_exists = await redis_client.exists(f"chat:lock:{sid}")
    assert not lock_exists, "Session lock was not released"


# ---------------------------------------------------------------------------
# S2: StopWithAi — event set after content emitted
# ---------------------------------------------------------------------------
#
# Key design: fake stream yields a content delta first, then looks up the
# event from running_streams and sets it BEFORE yielding the next payload.
# The generator's stop check fires on the second iteration → StopWithAi.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_with_ai(
    api_client_with_eval, auth_headers_child, db_session, redis_client,
):
    """StopWithAi: event set after first delta → stopped with aid + DB ai + 'user_stopped'."""
    headers, child = auth_headers_child

    async def fake_astream_with_stop(initial_state, stream_mode="custom"):
        yield {"delta": "Hello"}  # first payload: has_emitted_content = True
        # Generator processes this payload (accumulate, yield SSE),
        # then calls __anext__() — resume here.
        # Set the stop event BEFORE yielding the next payload.
        sid = initial_state["session_id"]
        ev = running_streams.get(sid)
        if ev is not None:
            ev.set()
        yield {"finish_reason": "stop"}
        # Generator: no delta, stop check fires → StopWithAi

    with patch.object(main_graph, "astream", fake_astream_with_stop):
        body = make_payload(content="Hi")
        resp = await api_client_with_eval.post(
            "/api/v1/me/chat/stream", json=body, headers=headers,
        )
        assert resp.status_code == 200
        await resp.aclose()

    frames = _parse_sse_frames(resp.text)
    frame_types = [f["type"] for f in frames]

    assert "session_meta" in frame_types
    assert "stopped" in frame_types, f"No stopped frame in {frame_types}"
    assert "end" not in frame_types, "Unexpected end frame in StopWithAi path"

    stopped_frame = next(f for f in frames if f["type"] == "stopped")
    assert stopped_frame["data"]["finish_reason"] == "user_stopped"
    # StopWithAi: aid is present
    assert stopped_frame["data"].get("aid") is not None, "StopWithAi should have aid"

    sid = frames[0]["data"]["session_id"]

    # DB: human (decision matrix) + ai (StopWithAi persist)
    msgs = (
        (await db_session.execute(
            select(Message).where(Message.session_id == sid).order_by(Message.created_at),
        ))
        .scalars()
        .all()
    )
    assert len(msgs) == 2, f"Expected 2 rows (human + ai), got {len(msgs)}"
    ai_msg = msgs[1]
    assert ai_msg.role == MessageRole.ai
    assert ai_msg.status == MessageStatus.active
    # 关注点2+6: finish_reason 强制覆盖为 'user_stopped'（非 SDK 透传值）
    assert ai_msg.finish_reason == "user_stopped", (
        f"Expected 'user_stopped', got '{ai_msg.finish_reason}'"
    )
    assert ai_msg.content == "Hello"

    # running_streams cleanup
    assert sid not in running_streams, "running_streams entry was not cleaned up"

    # Lock released
    lock_exists = await redis_client.exists(f"chat:lock:{sid}")
    assert not lock_exists, "Session lock was not released"


# ---------------------------------------------------------------------------
# S3: KeepGo (不 cancel) — ConnectionError at yield, LLM stream continues
# ---------------------------------------------------------------------------
#
# Key design: mock stream_graph_to_sse to raise ConnectionError after the
# first successful delta.  The generator's inner try/except catches it,
# sets client_alive=False, and continues consuming the graph stream.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keepgo_connection_error(
    api_client_with_eval, auth_headers_child, db_session, redis_client,
):
    """ConnectionError at SSE yield → LLM stream continues, all chunks consumed, DB written."""
    headers, child = auth_headers_child

    consumed_count = 0
    fake_payloads = [
        {"delta": "你"},
        {"delta": "好"},
        {"delta": "！"},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom"):
        nonlocal consumed_count
        for p in fake_payloads:
            consumed_count += 1
            yield p

    sse_call_count = 0

    async def mock_stream_to_sse(payloads):
        nonlocal sse_call_count
        sse_call_count += 1
        async for p in payloads:
            if sse_call_count >= 2:  # first call succeeds, second+ raise
                raise ConnectionError("mock client disconnect")
            yield _make_delta_frame(p.get("delta", ""))

    with patch.object(main_graph, "astream", fake_astream):
        with patch("app.api.me.stream_graph_to_sse", mock_stream_to_sse):
            body = make_payload(content="Hi")
            resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers,
            )
            assert resp.status_code == 200
            await resp.aclose()

    # 关注点4+6: 所有 fake stream chunk 均被消费（不 cancel 语义）
    assert consumed_count == len(fake_payloads), (
        f"Expected {len(fake_payloads)} chunks consumed, got {consumed_count}"
    )

    frames = _parse_sse_frames(resp.text)
    frame_types = [f["type"] for f in frames]
    assert "session_meta" in frame_types

    sid = frames[0]["data"]["session_id"]

    # DB: accumulated = "你好！" (all deltas concatenated), finish_reason = "stop"
    msgs = (
        (await db_session.execute(
            select(Message).where(Message.session_id == sid).order_by(Message.created_at),
        ))
        .scalars()
        .all()
    )
    assert len(msgs) == 2, f"Expected 2 rows (human + ai), got {len(msgs)}"
    ai_msg = msgs[1]
    assert ai_msg.role == MessageRole.ai
    assert ai_msg.content == "你好！", f"Expected '你好！', got '{ai_msg.content}'"
    assert ai_msg.finish_reason == "stop"

    # running_streams cleanup
    assert sid not in running_streams, "running_streams entry was not cleaned up"

    # 关注点6: 锁释放 — 锁 key 不存在
    lock_exists = await redis_client.exists(f"chat:lock:{sid}")
    assert not lock_exists, "Session lock was not released after keepgo"


# ---------------------------------------------------------------------------
# S4: running_streams cleanup after normal stream (no stop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_streams_cleaned_after_normal_end(
    api_client_with_eval, auth_headers_child, db_session,
):
    """Natural stream end: running_streams entry is removed in finally."""
    headers, child = auth_headers_child

    async def fake_astream(initial_state, stream_mode="custom"):
        yield {"delta": "Normal"}
        yield {"finish_reason": "stop"}

    with patch.object(main_graph, "astream", fake_astream):
        body = make_payload(content="Hi")
        resp = await api_client_with_eval.post(
            "/api/v1/me/chat/stream", json=body, headers=headers,
        )
        assert resp.status_code == 200
        await resp.aclose()

    frames = _parse_sse_frames(resp.text)
    sid = frames[0]["data"]["session_id"]

    # running_streams entry must be cleaned up
    assert sid not in running_streams, "running_streams was not cleaned after normal end"


# ---------------------------------------------------------------------------
# S5: running_streams cleanup after error path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_streams_cleaned_after_error(
    api_client_with_eval, auth_headers_child, db_session,
):
    """Error path: running_streams entry is removed in finally."""
    headers, child = auth_headers_child

    async def fake_astream_broken(initial_state, stream_mode="custom"):
        yield {"delta": "before error"}
        raise RuntimeError("graph failure")

    with patch.object(main_graph, "astream", fake_astream_broken):
        body = make_payload(content="Hi")
        resp = await api_client_with_eval.post(
            "/api/v1/me/chat/stream", json=body, headers=headers,
        )
        assert resp.status_code == 200
        await resp.aclose()

    frames = _parse_sse_frames(resp.text)
    sid = frames[0]["data"]["session_id"]

    assert sid not in running_streams, "running_streams was not cleaned after error"


# ---------------------------------------------------------------------------
# S6: Inner yield-frame except path — BrokenResourceError via athrow()
# ---------------------------------------------------------------------------
# Step 8c advisory #1 覆盖：yield frame 处的内层 except 捕获
# anyio.BrokenResourceError，设置 client_alive=False 并继续消费 LLM 流。
#
# 此路径无法通过 httpx ASGI transport 从外部触发（断连发 GeneratorExit 而非
# BrokenResourceError），故借助 _BrokenOnCall wrapper 在第 3 次 __anext__
# 时通过 athrow() 将异常注入生成器暂停点。
#
# 断言三连：
#   1) LLM 流全部消费完（consumed_count == len(fake_payloads)）
#   2) DB ai 行写入完成，content/finish_reason 正确
#   3) 双锁释放 + running_streams 清理
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keepgo_inner_yield_connection_error(
    api_client_with_eval, auth_headers_child, db_session, redis_client,
):
    """Inner ``yield frame`` catches BrokenResourceError via athrow → client_alive=False.

    Triple assertion: all payloads consumed, DB written correctly,
    locks released.
    """
    from starlette.responses import StreamingResponse

    headers, child = auth_headers_child

    fake_payloads = [
        {"delta": "你"},
        {"delta": "好"},
        {"delta": "！"},
        {"finish_reason": "stop"},
    ]
    consumed_count = 0

    async def fake_astream(initial_state, stream_mode="custom"):
        nonlocal consumed_count
        for p in fake_payloads:
            consumed_count += 1
            yield p

    # Patch StreamingResponse.__init__ to wrap body_iterator with _BrokenOnCall
    original_sr_init = StreamingResponse.__init__

    def patched_sr_init(self, content, *args, **kwargs):
        original_sr_init(self, content, *args, **kwargs)
        # break_after=2 → inject on call 3 (after session_meta + first delta)
        self.body_iterator = _BrokenOnCall(self.body_iterator, break_after=2)

    with patch.object(StreamingResponse, "__init__", patched_sr_init):
        with patch.object(main_graph, "astream", fake_astream):
            body = {"content": "Hi"}
            resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers,
            )
            assert resp.status_code == 200
            # 读取完整 body — 生成器在后台处理所有 payload 后结束
            _ = resp.text

    # 断言 1: LLM 流全部消费完
    assert consumed_count == len(fake_payloads), (
        f"Expected {len(fake_payloads)} chunks consumed, got {consumed_count}"
    )

    frames = _parse_sse_frames(resp.text)
    sid = frames[0]["data"]["session_id"]

    # 断言 2: DB ai 行写入完成
    msgs = (
        (await db_session.execute(
            select(Message).where(Message.session_id == sid).order_by(Message.created_at),
        ))
        .scalars()
        .all()
    )
    assert len(msgs) == 2, f"Expected 2 rows (human + ai), got {len(msgs)}"
    ai_msg = msgs[1]
    assert ai_msg.role == MessageRole.ai
    assert ai_msg.status == MessageStatus.active
    assert ai_msg.content == "你好！", f"Expected '你好！', got '{ai_msg.content}'"
    assert ai_msg.finish_reason == "stop"

    # 断言 3: 双锁释放 + running_streams 清理
    lock_exists = await redis_client.exists(f"chat:lock:{sid}")
    assert not lock_exists, "Session lock was not released"

    assert sid not in running_streams, "running_streams entry was not cleaned up"
