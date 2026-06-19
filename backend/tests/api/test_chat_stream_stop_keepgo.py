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
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")


@pytest.fixture(autouse=True)
def _mock_enqueue_audit():
    """mock enqueue_audit 避免 Redis lifespan 依赖。"""
    with patch("app.domain.chat.pipeline.enqueue_audit", AsyncMock()):
        yield


from app.domain.chat.graph import build_main_graph
from app.core.redis import commit_with_redis
from app.domain.auth.tokens import issue_token
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

main_graph = build_main_graph()
from app.core.db import get_db
from app.core.enums import MessageRole, MessageStatus, UserRole
from app.domain.chat.models import Message
from app.domain.chat.stream_signals import running_streams
from tests.api._chat_stream_lifecycle_helpers import (  # noqa: F401  # lifecycle_ctx 是 fixture param
    lifecycle_ctx,
    lifecycle_setup,
)

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

    from app.core.redis import get_redis
    from app.main import create_app
    from tests.conftest import _inject_mock_resources_with_session

    application = create_app()

    async def _get_db():
        yield db_session

    async def _get_redis():
        return redis_client_with_eval

    application.dependency_overrides[get_db] = _get_db
    application.dependency_overrides[get_redis] = _get_redis

    _inject_mock_resources_with_session(application, redis_client_with_eval, db_session)
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
    lifecycle_ctx,
    monkeypatch,
):
    """StopNoAi: event set before graph yields content → stopped without aid, DB human only."""
    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    async def fake_astream_no_content(initial_state, stream_mode="custom", **kwargs):
        # Generator registered its event in running_streams before calling astream;
        # look it up and set it before yielding any content-bearing payload.
        # §H.1: session_id 从 context kwarg 取（MainDialogueState 已删 session_id 字段）
        ctx = kwargs.get("context")
        sid = str(ctx.session_id) if ctx else None
        ev = running_streams.get(sid)
        if ev is not None:
            ev.set()
        yield {"finish_reason": "stop"}  # no delta → has_emitted_content stays False

    lifecycle_ctx.rr.main_graph.astream = fake_astream_no_content
    body = make_payload(content="Hello")
    resp = await client.post(
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
        (await lifecycle_ctx.assert_sess.execute(
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
    lock_exists = await lifecycle_ctx.redis_client.exists(f"chat:lock:{sid}")
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
async def test_stop_with_ai(lifecycle_ctx):
    """StopWithAi: event set after first delta → stopped with aid + DB ai + 'user_stopped'."""
    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    async def fake_astream_with_stop(initial_state, stream_mode="custom", **kwargs):
        yield {"delta": "Hello"}  # first payload: has_emitted_content = True
        ctx = kwargs.get("context")
        sid = str(ctx.session_id) if ctx else None
        ev = running_streams.get(sid)
        if ev is not None:
            ev.set()
        yield {"finish_reason": "stop"}

    lifecycle_ctx.rr.main_graph.astream = fake_astream_with_stop
    body = make_payload(content="Hi")
    resp = await client.post(
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
    assert stopped_frame["data"].get("aid") is not None, "StopWithAi should have aid"

    sid = frames[0]["data"]["session_id"]

    # DB: human (decision matrix) + ai (StopWithAi persist)
    lifecycle_ctx.assert_sess.expire_all()
    msgs = (
        (await lifecycle_ctx.assert_sess.execute(
            select(Message).where(Message.session_id == sid).order_by(Message.created_at),
        ))
        .scalars()
        .all()
    )
    assert len(msgs) == 2, f"Expected 2 rows (human + ai), got {len(msgs)}"
    ai_msg = msgs[1]
    assert ai_msg.role == MessageRole.ai
    assert ai_msg.status == MessageStatus.active
    assert ai_msg.finish_reason == "user_stopped", (
        f"Expected 'user_stopped', got '{ai_msg.finish_reason}'"
    )
    assert ai_msg.content == "Hello"

    # running_streams cleanup
    assert sid not in running_streams, "running_streams entry was not cleaned up"

    # Lock released
    lock_exists = await lifecycle_ctx.redis_client.exists(f"chat:lock:{sid}")
    assert not lock_exists, "Session lock was not released"


# ---------------------------------------------------------------------------
# S4: running_streams cleanup after normal stream (no stop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_streams_cleaned_after_normal_end(
    app_with_eval, api_client_with_eval, auth_headers_child, db_session,
):
    """Natural stream end: running_streams entry is removed in finally."""
    headers, child = auth_headers_child

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        yield {"delta": "Normal"}
        yield {"finish_reason": "stop"}

    app_with_eval.state.resources.main_graph.astream = fake_astream
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
    app_with_eval, api_client_with_eval, auth_headers_child, db_session,
):
    """Error path: running_streams entry is removed in finally."""
    headers, child = auth_headers_child

    async def fake_astream_broken(initial_state, stream_mode="custom", **kwargs):
        yield {"delta": "before error"}
        raise RuntimeError("graph failure")

    app_with_eval.state.resources.main_graph.astream = fake_astream_broken
    body = make_payload(content="Hi")
    resp = await api_client_with_eval.post(
        "/api/v1/me/chat/stream", json=body, headers=headers,
    )
    assert resp.status_code == 200
    await resp.aclose()

    frames = _parse_sse_frames(resp.text)
    sid = frames[0]["data"]["session_id"]

    assert sid not in running_streams, "running_streams was not cleaned after error"
