"""Tests for POST /me/chat/stream graph integration (Step 8b).

Verifies:
- SSE 3-event sequence: session_meta → delta×N → end (no reasoning_content)
- T5 single-write: inline code writes exactly one ai active row (status='active', role='ai')
  with accumulated content and finish_reason from last graph chunk
- finish_reason three-state coverage: stop / length / content_filter
- error path: SSE error frame + human active row retained + no ai row written + lock released
- T5 commit: db.commit() called after AI message creation (cross-connection persistence)
- 8a control plane regression (decision matrix, locks, title)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

pytestmark = pytest.mark.asyncio(loop_scope="function")


@pytest.fixture(autouse=True)
def _mock_enqueue_audit():
    """mock enqueue_audit 避免 Redis lifespan 依赖。"""
    with patch("app.domain.chat.pipeline.enqueue_audit", AsyncMock()):
        yield


from app.auth.tokens import issue_token
from app.chat.graph import build_main_graph
from app.core.redis import commit_with_redis
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

main_graph = build_main_graph()
from app.core.db import get_db
from app.core.enums import MessageRole, MessageStatus, UserRole
from app.models.chat import Message
from app.models.chat import Session as SessionModel
from tests.api._chat_stream_lifecycle_helpers import (
    lifecycle_ctx,  # noqa: F401  # fixture param
    lifecycle_setup,
    seed_compression_session,
)

# ---------------------------------------------------------------------------
# Fixtures (reused from test_chat_stream_control_plane.py)
# ---------------------------------------------------------------------------


@pytest.fixture
async def redis_client_with_eval(redis_client: FakeRedis) -> FakeRedis:
    """Patch FakeRedis.eval to simulate Lua DEL-if-nonce-match."""
    import fakeredis.aioredis

    async def mock_eval(self, script: str, num_keys: int, key: str, nonce_arg: str) -> int:
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
    from tests.conftest import _inject_mock_resources

    application = create_app()

    async def _get_db():
        yield db_session

    async def _get_redis():
        return redis_client_with_eval

    application.dependency_overrides[get_db] = _get_db
    application.dependency_overrides[get_redis] = _get_redis

    _inject_mock_resources(application, redis_client_with_eval)
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
    device_id = "test-device-8b"
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
    import json as _json

    events: list[dict] = []
    current_type = None
    for line in raw.split("\n"):
        if line.startswith("event:"):
            current_type = line[len("event:") :].strip()
        elif line.startswith("data:") and current_type is not None:
            data_str = line[len("data:") :].strip()
            events.append({"type": current_type, "data": _json.loads(data_str)})
    return events


# ---------------------------------------------------------------------------
# G1: SSE 7-event sequence — normal graph stream
# ---------------------------------------------------------------------------


class _FakeGraphStream:
    """Wrap a list of dict payloads as an async iterator (simulates custom-mode stream)."""

    def __init__(self, payloads: list[dict]):
        self._payloads = payloads

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._payloads:
            return self._payloads.pop(0)
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_sse_sequence_session_meta_multi_delta_end(lifecycle_ctx):
    """session_meta → delta×2 → end (multi-delta, no reasoning_content)."""
    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    fake_payloads = [
        {"delta": "你"},
        {"delta": "好"},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for p in fake_payloads:
            yield p

    lifecycle_ctx.rr.main_graph.astream = fake_astream
    body = make_payload(content="Hello")
    resp = await client.post("/api/v1/me/chat/stream", json=body, headers=headers)
    assert resp.status_code == 200
    await resp.aclose()

    frames = _parse_sse_frames(resp.text)
    frame_types = [f["type"] for f in frames]

    assert frame_types[0] == "session_meta"
    assert frame_types[1] == "delta"
    assert frame_types[2] == "delta"
    assert frame_types[3] == "end"
    assert frames[3]["data"]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_sse_sequence_delta_only_no_reasoning(
    app_with_eval, api_client_with_eval, auth_headers_child, db_session, monkeypatch
):
    """No reasoning_content → thinking_start/thinking_end not emitted; only delta + end."""
    headers, child = auth_headers_child

    fake_payloads = [
        {"delta": "Hello"},  # no reasoning
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for p in fake_payloads:
            yield p

    app_with_eval.state.resources.main_graph.astream = fake_astream

    body = make_payload(content="Hi")
    resp = await api_client_with_eval.post("/api/v1/me/chat/stream", json=body, headers=headers)
    assert resp.status_code == 200

    frames = _parse_sse_frames(resp.text)
    frame_types = [f["type"] for f in frames]

    assert frame_types[0] == "session_meta"
    assert "thinking_start" not in frame_types
    assert "thinking_end" not in frame_types
    assert "delta" in frame_types
    assert "end" in frame_types


# ---------------------------------------------------------------------------
# G2: T5 single-write — finish_reason three-state coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t5_writes_ai_active_with_stop(lifecycle_ctx):
    """Inline AI write on normal path → ai active row + finish_reason='stop'."""
    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    fake_payloads = [
        {"delta": "回复内容"},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for p in fake_payloads:
            yield p

    lifecycle_ctx.rr.main_graph.astream = fake_astream

    body = make_payload(content="Hello")
    resp = await client.post("/api/v1/me/chat/stream", json=body, headers=headers)
    assert resp.status_code == 200
    await resp.aclose()

    sid = _parse_sse_frames(resp.text)[0]["data"]["session_id"]

    lifecycle_ctx.assert_sess.expire_all()
    msgs = (
        (
            await lifecycle_ctx.assert_sess.execute(
                select(Message).where(Message.session_id == sid).order_by(Message.created_at)
            )
        )
        .scalars()
        .all()
    )

    assert len(msgs) == 2
    human_msg, ai_msg = msgs
    assert human_msg.role == MessageRole.human
    assert human_msg.status == MessageStatus.active
    assert ai_msg.role == MessageRole.ai
    assert ai_msg.status == MessageStatus.active
    assert ai_msg.content == "回复内容"
    assert ai_msg.finish_reason == "stop"

    end_frame = next(f for f in _parse_sse_frames(resp.text) if f["type"] == "end")
    assert end_frame["data"]["finish_reason"] == "stop"
    assert end_frame["data"]["aid"] == str(ai_msg.id)


@pytest.mark.asyncio
async def test_t5_finish_reason_length(lifecycle_ctx):
    """Mock last chunk finish_reason='length' → DB row + SSE end frame both have 'length'."""
    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    fake_payloads = [
        {"delta": "长回复"},
        {"finish_reason": "length"},
    ]

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for p in fake_payloads:
            yield p

    lifecycle_ctx.rr.main_graph.astream = fake_astream

    body = make_payload(content="Tell me a story")
    resp = await client.post("/api/v1/me/chat/stream", json=body, headers=headers)
    assert resp.status_code == 200
    await resp.aclose()

    sid = _parse_sse_frames(resp.text)[0]["data"]["session_id"]
    lifecycle_ctx.assert_sess.expire_all()
    msgs = (
        (
            await lifecycle_ctx.assert_sess.execute(
                select(Message).where(Message.session_id == sid, Message.role == MessageRole.ai)
            )
        )
        .scalars()
        .all()
    )

    assert len(msgs) == 1
    assert msgs[0].finish_reason == "length"

    end_frame = next(f for f in _parse_sse_frames(resp.text) if f["type"] == "end")
    assert end_frame["data"]["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_t5_finish_reason_content_filter(lifecycle_ctx):
    """finish_reason='content_filter' → DB row + SSE end frame both have 'content_filter'."""
    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    fake_payloads = [
        {"delta": "filtered"},
        {"finish_reason": "content_filter"},
    ]

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for p in fake_payloads:
            yield p

    lifecycle_ctx.rr.main_graph.astream = fake_astream

    body = make_payload(content="Test filter")
    resp = await client.post("/api/v1/me/chat/stream", json=body, headers=headers)
    assert resp.status_code == 200
    await resp.aclose()

    sid = _parse_sse_frames(resp.text)[0]["data"]["session_id"]
    lifecycle_ctx.assert_sess.expire_all()
    msgs = (
        (
            await lifecycle_ctx.assert_sess.execute(
                select(Message).where(Message.session_id == sid, Message.role == MessageRole.ai)
            )
        )
        .scalars()
        .all()
    )

    assert len(msgs) == 1
    assert msgs[0].finish_reason == "content_filter"

    end_frame = next(f for f in _parse_sse_frames(resp.text) if f["type"] == "end")
    assert end_frame["data"]["finish_reason"] == "content_filter"


# ---------------------------------------------------------------------------
# G3: T5 commit — db.commit() called inside generator (P0-A verification)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t5_commit_called_after_persist(lifecycle_ctx):
    """Generator calls db.commit() at least twice: decision-matrix commit① + AI-row commit②."""
    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    fake_payloads = [
        {"delta": "hi"},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for p in fake_payloads:
            yield p

    lifecycle_ctx.rr.main_graph.astream = fake_astream

    body = make_payload(content="Hello")
    resp = await client.post("/api/v1/me/chat/stream", json=body, headers=headers)
    assert resp.status_code == 200
    await resp.aclose()

    # Verify commit② happened: ai row should be visible via cross-connection read
    sid = _parse_sse_frames(resp.text)[0]["data"]["session_id"]
    lifecycle_ctx.assert_sess.expire_all()
    ai_msgs = (await lifecycle_ctx.assert_sess.execute(
        select(Message).where(Message.session_id == sid, Message.role == MessageRole.ai)
    )).scalars().all()
    assert len(ai_msgs) == 1, "commit② should have written exactly one ai row"


@pytest.mark.asyncio
async def test_ai_row_persisted_cross_connection(lifecycle_ctx):
    """After generator commit②, AI row is visible from a completely new DB connection."""
    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    fake_payloads = [
        {"delta": "跨连接回复"},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for p in fake_payloads:
            yield p

    lifecycle_ctx.rr.main_graph.astream = fake_astream

    body = make_payload(content="Hello")
    resp = await client.post("/api/v1/me/chat/stream", json=body, headers=headers)
    assert resp.status_code == 200
    await resp.aclose()

    sid = _parse_sse_frames(resp.text)[0]["data"]["session_id"]

    # Read via assert_sess (separate connection) — proves real commit② visibility
    lifecycle_ctx.assert_sess.expire_all()
    result = await lifecycle_ctx.assert_sess.execute(
        select(Message.content, Message.finish_reason).where(
            Message.session_id == sid, Message.role == MessageRole.ai
        )
    )
    row = result.one_or_none()
    assert row is not None, "AI row not visible — commit② may not have executed"
    assert row[0] == "跨连接回复"
    assert row[1] == "stop"


# ---------------------------------------------------------------------------
# G3: Error path — error frame + human active + no ai row + lock released + persist NOT called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_path_emits_error_frame_and_preserves_human(lifecycle_ctx):
    """graph raises Exception → SSE error frame + human active row retained + no ai row."""
    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    async def fake_astream_broken(initial_state, stream_mode="custom", **kwargs):
        yield {"delta": "partial before error"}
        raise RuntimeError("graph internal error")

    lifecycle_ctx.rr.main_graph.astream = fake_astream_broken

    body = make_payload(content="Hello")
    resp = await client.post("/api/v1/me/chat/stream", json=body, headers=headers)
    assert resp.status_code == 200
    await resp.aclose()

    frames = _parse_sse_frames(resp.text)
    frame_types = [f["type"] for f in frames]

    assert "error" in frame_types, f"No error frame in {frame_types}"
    error_frame = next(f for f in frames if f["type"] == "error")
    assert "graph internal error" in error_frame["data"]["message"]

    sid = frames[0]["data"]["session_id"]
    lifecycle_ctx.assert_sess.expire_all()
    msgs = (
        (
            await lifecycle_ctx.assert_sess.execute(
                select(Message).where(Message.session_id == sid).order_by(Message.created_at)
            )
        )
        .scalars()
        .all()
    )

    # exactly 1 row: human active (orphan or new, no ai row)
    assert len(msgs) == 1, f"Expected 1 row (human only), got {len(msgs)}: {msgs}"
    assert msgs[0].role == MessageRole.human
    assert msgs[0].status == MessageStatus.active


@pytest.mark.asyncio
async def test_error_path_lock_released(
    app_with_eval, api_client_with_eval, auth_headers_child, db_session, redis_client
):
    """Error path: session lock must be released even when graph raises."""
    headers, child = auth_headers_child

    async def fake_astream_broken(initial_state, stream_mode="custom", **kwargs):
        yield {"delta": "partial"}
        raise RuntimeError("graph error")

    app_with_eval.state.resources.main_graph.astream = fake_astream_broken

    body = make_payload(content="Hello")
    resp = await api_client_with_eval.post("/api/v1/me/chat/stream", json=body, headers=headers)
    assert resp.status_code == 200
    await resp.aclose()

    sid = _parse_sse_frames(resp.text)[0]["data"]["session_id"]

    # Lock key should not exist after generator finishes
    lock_exists = await redis_client.exists(f"chat:lock:{sid}")
    assert not lock_exists, "Session lock was not released after error"


# ---------------------------------------------------------------------------
# G4: Accumulated content correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accumulated_content_concatenates_all_deltas(lifecycle_ctx):
    """accumulated = sum of all delta payloads → ai row content = full concatenated text."""
    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    fake_payloads = [
        {"delta": "你"},
        {"delta": "好"},
        {"delta": "！"},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for p in fake_payloads:
            yield p

    lifecycle_ctx.rr.main_graph.astream = fake_astream

    body = make_payload(content="Hi")
    resp = await client.post("/api/v1/me/chat/stream", json=body, headers=headers)
    assert resp.status_code == 200
    await resp.aclose()

    sid = _parse_sse_frames(resp.text)[0]["data"]["session_id"]
    lifecycle_ctx.assert_sess.expire_all()
    ai_msg = (
        await lifecycle_ctx.assert_sess.execute(
            select(Message).where(Message.session_id == sid, Message.role == MessageRole.ai)
        )
    ).scalar_one_or_none()

    assert ai_msg is not None
    assert ai_msg.content == "你好！"


# ---------------------------------------------------------------------------
# G5: Aid returned and used in end frame
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_frame_contains_real_aid(lifecycle_ctx):
    """emit_end receives real aid from inline AI message id."""
    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    fake_payloads = [
        {"delta": "response"},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for p in fake_payloads:
            yield p

    lifecycle_ctx.rr.main_graph.astream = fake_astream

    body = make_payload(content="Hello")
    resp = await client.post("/api/v1/me/chat/stream", json=body, headers=headers)
    assert resp.status_code == 200
    await resp.aclose()

    sid = _parse_sse_frames(resp.text)[0]["data"]["session_id"]
    lifecycle_ctx.assert_sess.expire_all()
    ai_msg = (
        await lifecycle_ctx.assert_sess.execute(
            select(Message).where(Message.session_id == sid, Message.role == MessageRole.ai)
        )
    ).scalar_one_or_none()

    end_frame = next(f for f in _parse_sse_frames(resp.text) if f["type"] == "end")
    assert end_frame["data"]["aid"] == str(ai_msg.id)
    assert end_frame["data"]["aid"] is not None


# ---------------------------------------------------------------------------
# G6: thinking_start/end SSE sequence via reasoning payloads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_start_end_sse_sequence(lifecycle_ctx):
    """reasoning signal payloads → thinking_start (once) → first delta → thinking_end."""
    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    fake_payloads = [
        {"reasoning": True},
        {"reasoning": True},
        {"delta": "你好"},
        {"delta": "！"},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for p in fake_payloads:
            yield p

    lifecycle_ctx.rr.main_graph.astream = fake_astream
    body = make_payload(content="Hello")
    resp = await client.post(
        "/api/v1/me/chat/stream", json=body, headers=headers
    )
    assert resp.status_code == 200
    await resp.aclose()

    frames = _parse_sse_frames(resp.text)
    frame_types = [f["type"] for f in frames]

    assert frame_types == [
        "session_meta",
        "thinking_start",
        "thinking_end",
        "delta",
        "delta",
        "end",
    ], f"Unexpected frame sequence: {frame_types}"


@pytest.mark.asyncio
async def test_thinking_no_reasoning_no_signals(
    app_with_eval, api_client_with_eval, auth_headers_child, db_session
):
    """No reasoning payloads → no thinking_start/thinking_end frames."""
    headers, child = auth_headers_child

    fake_payloads = [
        {"delta": "你好"},
        {"delta": "！"},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for p in fake_payloads:
            yield p

    app_with_eval.state.resources.main_graph.astream = fake_astream
    body = make_payload(content="Hello")
    resp = await api_client_with_eval.post(
        "/api/v1/me/chat/stream", json=body, headers=headers
    )
    assert resp.status_code == 200
    await resp.aclose()

    frames = _parse_sse_frames(resp.text)
    frame_types = [f["type"] for f in frames]

    assert "thinking_start" not in frame_types
    assert "thinking_end" not in frame_types
    assert frame_types[0] == "session_meta"
    assert frame_types[-1] == "end"


@pytest.mark.asyncio
async def test_thinking_only_no_content_no_emit_end(
    app_with_eval, api_client_with_eval, auth_headers_child, db_session
):
    """Only reasoning payloads, no delta → thinking_start but NO thinking_end."""
    headers, child = auth_headers_child

    fake_payloads = [
        {"reasoning": True},
        {"reasoning": True},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for p in fake_payloads:
            yield p

    app_with_eval.state.resources.main_graph.astream = fake_astream
    body = make_payload(content="Hello")
    resp = await api_client_with_eval.post(
        "/api/v1/me/chat/stream", json=body, headers=headers
    )
    assert resp.status_code == 200
    await resp.aclose()

    frames = _parse_sse_frames(resp.text)
    frame_types = [f["type"] for f in frames]
    assert "thinking_start" in frame_types
    assert "thinking_end" not in frame_types, (
        f"thinking_end should NOT be emitted without delta; got {frame_types}"
    )


# ---------------------------------------------------------------------------
# G7: Compression path — session_meta → compression_start → compression_end → delta → end
# ---------------------------------------------------------------------------


@pytest.fixture
async def compression_session(db_session, child_user):
    """Create a session with 2 active messages + needs_compression=True."""
    from datetime import UTC
    from datetime import datetime as _dt
    from uuid import uuid4 as _uuid4

    base_ts = _dt.now(UTC)
    sid = _uuid4()
    session = SessionModel(id=sid, child_user_id=child_user.id, title="test")
    db_session.add(session)
    await db_session.flush()

    msg1 = Message(
        session_id=sid, role=MessageRole.human,
        content="你好", status=MessageStatus.active,
    )
    msg1.created_at = base_ts
    db_session.add(msg1)
    await db_session.flush()

    msg2 = Message(
        session_id=sid, role=MessageRole.ai,
        content="今天天气不错", status=MessageStatus.active,
    )
    msg2.created_at = base_ts.replace(microsecond=base_ts.microsecond + 1)
    db_session.add(msg2)
    await db_session.flush()

    session.needs_compression = True
    session.context_size_tokens = 600000
    await db_session.commit()
    return sid, child_user, msg1.id, msg2.id


@pytest.mark.asyncio
async def test_compression_normal_path(
    lifecycle_ctx,
):
    """Compression normal path: session_meta → compression_start → compression_end → delta → end."""
    from unittest.mock import AsyncMock, patch

    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid, _, msg1_id, msg2_id = await seed_compression_session(lifecycle_ctx, child)

    fake_payloads = [
        {"delta": "回复"},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for p in fake_payloads:
            yield p

    fake_c_llm = AsyncMock()
    fake_c_llm.ainvoke = AsyncMock(return_value=AIMessage(content="用户打招呼，AI 回应天气"))

    lifecycle_ctx.rr.main_graph.astream = fake_astream

    with patch("app.core.llm.build_provider_llm", return_value=fake_c_llm):
        body = make_payload(content="继续聊聊", session_id=str(sid))
        resp = await client.post(
            "/api/v1/me/chat/stream", json=body, headers=headers,
        )
        assert resp.status_code == 200
        await resp.aclose()

    frames = _parse_sse_frames(resp.text)
    frame_types = [f["type"] for f in frames]

    # Event sequence: session_meta → compression_start → compression_end → delta → end
    assert frame_types[0] == "session_meta"
    assert frame_types[1] == "compression_start"
    assert frame_types[2] == "compression_end"
    assert frame_types[3] == "delta"
    assert frame_types[4] == "end"

    lifecycle_ctx.assert_sess.expire_all()
    summary = (
        await lifecycle_ctx.assert_sess.execute(
            select(Message).where(
                Message.session_id == sid, Message.role == MessageRole.summary,
            )
        )
    ).scalar_one_or_none()
    assert summary is not None
    assert summary.status == MessageStatus.active
    assert "用户打招呼" in summary.content

    for mid in (msg1_id, msg2_id):
        row = await lifecycle_ctx.assert_sess.get(Message, mid)
        assert row is not None, f"Message {mid} not found"
        assert row.status == MessageStatus.compressed, f"msg {mid} status={row.status}"


@pytest.mark.asyncio
async def test_compression_with_reasoning_path(lifecycle_ctx):
    """Compression + reasoning: session_meta → compression_start → compression_end → thinking_start → thinking_end → delta → end."""
    from unittest.mock import AsyncMock, patch

    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid, _, msg1_id, msg2_id = await seed_compression_session(lifecycle_ctx, child)

    fake_payloads = [
        {"reasoning": True},
        {"reasoning": True},
        {"delta": "思考后回复"},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for p in fake_payloads:
            yield p

    fake_c_llm = AsyncMock()
    fake_c_llm.ainvoke = AsyncMock(return_value=AIMessage(content="摘要"))

    lifecycle_ctx.rr.main_graph.astream = fake_astream

    with patch("app.core.llm.build_provider_llm", return_value=fake_c_llm):
        body = make_payload(content="继续", session_id=str(sid))
        resp = await client.post(
            "/api/v1/me/chat/stream", json=body, headers=headers,
        )
    assert resp.status_code == 200
    await resp.aclose()

    frames = _parse_sse_frames(resp.text)
    frame_types = [f["type"] for f in frames]

    assert frame_types[0] == "session_meta"
    assert frame_types[1] == "compression_start"
    assert frame_types[2] == "compression_end"
    assert frame_types[3] == "thinking_start"
    assert frame_types[4] == "thinking_end"
    assert frame_types[5] == "delta"
    assert frame_types[6] == "end"


@pytest.mark.asyncio
async def test_compression_failure_path(
    app_with_eval, api_client_with_eval, auth_headers_child, db_session, compression_session, monkeypatch,
):
    """Compression LLM raises → compression_start without compression_end + error(CompressionError)."""
    from unittest.mock import AsyncMock

    headers, child = auth_headers_child
    sid, _, msg1_id, msg2_id = compression_session

    fake_c_llm = AsyncMock()
    fake_c_llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM compression failed"))

    async def _fake_astream_fail(initial_state, stream_mode="custom", **kwargs):
        yield {"finish_reason": "stop"}

    app_with_eval.state.resources.main_graph.astream = _fake_astream_fail

    with patch("app.core.llm.build_provider_llm", return_value=fake_c_llm):
        body = make_payload(content="继续", session_id=str(sid))
        resp = await api_client_with_eval.post(
            "/api/v1/me/chat/stream", json=body, headers=headers,
        )
    # The error is caught inside generator, so HTTP status is still 200
    assert resp.status_code == 200
    await resp.aclose()

    frames = _parse_sse_frames(resp.text)
    frame_types = [f["type"] for f in frames]

    # compression_start present, compression_end absent, error present
    assert frame_types[0] == "session_meta"
    assert "compression_start" in frame_types
    assert "compression_end" not in frame_types, (
        f"compression_end should NOT appear on failure; got {frame_types}"
    )
    assert "error" in frame_types
    error_frame = next(f for f in frames if f["type"] == "error")
    assert error_frame["data"]["code"] == "CompressionError"

    # No NEW AI row created by the generator（压缩失败 → commit② 未执行）
    _result = await db_session.execute(
        select(Message).where(
            Message.session_id == sid,
            Message.role == MessageRole.ai,
            Message.status == MessageStatus.active,
        )
    )
    ai_rows = _result.scalars().all()
    # 夹具已有 1 条 AI 消息（msg2），压缩失败后不应新增
    assert len(ai_rows) == 1, (
        f"Expected 1 pre-existing AI row, got {len(ai_rows)}"
    )

    # session.needs_compression should still be True (compression didn't complete)
    session_row = (
        await db_session.execute(
            select(SessionModel).where(SessionModel.id == sid)
        )
    ).scalar_one()
    assert session_row.needs_compression is True


@pytest.mark.asyncio
async def test_compression_row84_regression(lifecycle_ctx):
    """Row 84 回归断言：端到端压缩链路生成的 summary 不含对话式起首。

    Mock LLM 返回客观摘要「用户说你好，AI 回应今天天气不错」。
    断言存储的 summary 首 30 字符不含「好的」「嗯」「明白」「我准备」「小主人」。
    """
    from unittest.mock import AsyncMock

    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid, _, msg1_id, msg2_id = await seed_compression_session(lifecycle_ctx, child)

    fake_c_llm = AsyncMock()
    # Mock LLM returning a proper objective summary (NOT conversational greeting)
    fake_c_llm.ainvoke = AsyncMock(
        return_value=AIMessage(content="用户说你好，AI 回应今天天气不错"),
    )

    async def _fake_astream_84(initial_state, stream_mode="custom", **kwargs):
        yield {"delta": "r"}
        yield {"finish_reason": "stop"}

    lifecycle_ctx.rr.main_graph.astream = _fake_astream_84

    with patch("app.core.llm.build_provider_llm", return_value=fake_c_llm):
        body = make_payload(content="继续聊聊", session_id=str(sid))
        resp = await client.post(
            "/api/v1/me/chat/stream", json=body, headers=headers,
        )
    assert resp.status_code == 200
    await resp.aclose()

    # Verify summary content
    lifecycle_ctx.assert_sess.expire_all()
    summary = (
        await lifecycle_ctx.assert_sess.execute(
            select(Message).where(
                Message.session_id == sid, Message.role == MessageRole.summary,
            )
        )
    ).scalar_one_or_none()
    assert summary is not None, "Summary should exist"

    # Check first 30 chars for anti-pattern keywords
    first_30 = summary.content[:30]
    anti_keywords = ["好的", "嗯", "明白", "我准备", "小主人"]
    for kw in anti_keywords:
        assert kw not in first_30, (
            f"Summary contains conversational greeting '{kw}': first_30={first_30!r}"
        )


@pytest.mark.asyncio
async def test_compression_noop_empty_filter(lifecycle_ctx):
    """Only one active human message + needs_compression=True → actives empty after filter → noop (no summary, no compression markers)."""
    from uuid import uuid4 as _uuid4

    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid = _uuid4()

    session = SessionModel(id=sid, child_user_id=child.id, title="test")
    lifecycle_ctx.seed_sess.add(session)
    await lifecycle_ctx.seed_sess.flush()

    msg = Message(
        session_id=sid, role=MessageRole.human,
        content="你好", status=MessageStatus.active,
    )
    lifecycle_ctx.seed_sess.add(msg)
    await lifecycle_ctx.seed_sess.flush()

    session.needs_compression = True
    session.context_size_tokens = 600000
    await lifecycle_ctx.seed_sess.commit()

    fake_payloads = [
        {"delta": "回复你"},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for p in fake_payloads:
            yield p

    lifecycle_ctx.rr.main_graph.astream = fake_astream

    body = make_payload(content="新的消息", session_id=str(sid))
    resp = await client.post(
        "/api/v1/me/chat/stream", json=body, headers=headers,
    )
    assert resp.status_code == 200
    await resp.aclose()

    frames = _parse_sse_frames(resp.text)
    frame_types = [f["type"] for f in frames]

    # compression events should still appear (attempted, nothing to compress)
    assert "compression_start" in frame_types
    assert "compression_end" in frame_types

    # No summary row should be written
    lifecycle_ctx.assert_sess.expire_all()
    _result2 = await lifecycle_ctx.assert_sess.execute(
        select(Message).where(
            Message.session_id == sid, Message.role == MessageRole.summary,
        )
    )
    summaries = _result2.scalars().all()
    assert len(summaries) == 0, "No summary should exist in noop path"

    # session.needs_compression should be False (reset by noop path)
    session_row = (
        await lifecycle_ctx.assert_sess.execute(
            select(SessionModel).where(SessionModel.id == sid)
        )
    ).scalar_one()
    assert session_row.needs_compression is False


# ---------------------------------------------------------------------------
# G8: Compression messages order + secondary compression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compression_messages_order_assertion(lifecycle_ctx):
    """方案 a 核心契约：压缩后 initial_state["messages"] 顺序为 [system_prompt, summary, protected_human]."""
    from unittest.mock import AsyncMock
    from unittest.mock import patch as _patch

    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid, _, msg1_id, msg2_id = await seed_compression_session(lifecycle_ctx, child)

    captured: list = []
    summary_content = "测试摘要XYZ"

    async def spy_astream(initial_state, stream_mode="custom", **kwargs):
        captured.append(initial_state)
        yield {"delta": "回复"}
        yield {"finish_reason": "stop"}

    fake_c_llm = AsyncMock()
    fake_c_llm.ainvoke = AsyncMock(return_value=AIMessage(content=summary_content))

    lifecycle_ctx.rr.main_graph.astream = spy_astream

    with _patch("app.core.llm.build_provider_llm", return_value=fake_c_llm):
        body = make_payload(content="继续聊聊", session_id=str(sid))
        resp = await client.post(
            "/api/v1/me/chat/stream", json=body, headers=headers,
        )
        assert resp.status_code == 200
        await resp.aclose()

    # spy 必须被调用
    assert len(captured) == 1, f"expected 1 astream call, got {len(captured)}"

    msgs = captured[0]["messages"]
    # [main_system, summary, protected_human]
    assert len(msgs) == 3, f"expected 3 messages, got {len(msgs)}: {[type(m).__name__ for m in msgs]}"

    assert isinstance(msgs[0], SystemMessage), f"msgs[0] should be SystemMessage, got {type(msgs[0]).__name__}"
    assert msgs[0].content, "system prompt content should be non-empty"

    assert isinstance(msgs[1], SystemMessage), f"msgs[1] should be SystemMessage (summary), got {type(msgs[1]).__name__}"
    assert summary_content in msgs[1].content, (
        f"summary content {summary_content!r} not in msgs[1]: {msgs[1].content!r}"
    )

    assert isinstance(msgs[2], HumanMessage), f"msgs[2] should be HumanMessage (protected), got {type(msgs[2]).__name__}"
    assert msgs[2].content == "继续聊聊", (
        f"protected_human content mismatch: {msgs[2].content!r}"
    )

    assert [type(m).__name__ for m in msgs] == ["SystemMessage", "SystemMessage", "HumanMessage"], (
        f"type sequence mismatch: {[type(m).__name__ for m in msgs]}"
    )


@pytest.mark.asyncio
async def test_compression_with_existing_summary(lifecycle_ctx):
    """二次压缩：已有旧 summary 行的 session 中，旧 summary 被纳入压缩集并标 compressed。"""
    from datetime import UTC
    from datetime import datetime as _dt
    from unittest.mock import AsyncMock
    from unittest.mock import patch as _patch
    from uuid import uuid4 as _uuid4

    from app.chat.prompts import SUMMARY_PREFIX

    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid = _uuid4()

    session = SessionModel(id=sid, child_user_id=child.id, title="test")
    lifecycle_ctx.seed_sess.add(session)
    await lifecycle_ctx.seed_sess.flush()

    base_ts = _dt.now(UTC)
    rows_data = [
        (MessageRole.human, "第一轮"),
        (MessageRole.ai, "第一轮回复"),
        (MessageRole.summary, "上轮旧摘要"),
        (MessageRole.human, "第二轮"),
        (MessageRole.ai, "第二轮回复"),
    ]
    msg_ids = []
    for i, (role, content) in enumerate(rows_data):
        m = Message(
            session_id=sid, role=role,
            content=content if role != MessageRole.summary else SUMMARY_PREFIX + content,
            status=MessageStatus.active,
        )
        m.created_at = base_ts.replace(microsecond=base_ts.microsecond + i)
        lifecycle_ctx.seed_sess.add(m)
        await lifecycle_ctx.seed_sess.flush()
        msg_ids.append(m.id)

    session.needs_compression = True
    session.context_size_tokens = 600000
    await lifecycle_ctx.seed_sess.commit()

    captured: list = []

    async def spy_astream(initial_state, stream_mode="custom", **kwargs):
        captured.append(initial_state)
        yield {"delta": "回复"}
        yield {"finish_reason": "stop"}

    fake_c_llm = AsyncMock()
    fake_c_llm.ainvoke = AsyncMock(return_value=AIMessage(content="新合并摘要"))

    lifecycle_ctx.rr.main_graph.astream = spy_astream

    with _patch("app.core.llm.build_provider_llm", return_value=fake_c_llm):
        body = make_payload(content="第三轮", session_id=str(sid))
        resp = await client.post(
            "/api/v1/me/chat/stream", json=body, headers=headers,
        )
        assert resp.status_code == 200
        await resp.aclose()

    # 1) 5 行旧消息全部转 compressed（含旧 summary 行）
    lifecycle_ctx.assert_sess.expire_all()
    for mid in msg_ids:
        row = await lifecycle_ctx.assert_sess.get(Message, mid)
        assert row is not None, f"msg {mid} not found"
        assert row.status == MessageStatus.compressed, (
            f"msg {mid} (role={row.role}) status={row.status} — expected compressed"
        )

    # 2) 新 summary 行
    _result3 = await lifecycle_ctx.assert_sess.execute(
        select(Message).where(
            Message.session_id == sid,
            Message.role == MessageRole.summary,
            Message.status == MessageStatus.active,
        )
    )
    new_summaries = _result3.scalars().all()
    assert len(new_summaries) == 1, f"expected 1 new summary, got {len(new_summaries)}"
    assert "新合并摘要" in new_summaries[0].content, (
        f"summary content mismatch: {new_summaries[0].content!r}"
    )
    # 旧 summary 内容应被新摘要合并（不在 active summary 中单独出现）
    assert "上轮旧摘要" not in new_summaries[0].content, (
        "old summary text should be superseded, not appear verbatim in new summary"
    )

    # 3) session.needs_compression == False
    lifecycle_ctx.assert_sess.expire_all()
    session_row = await lifecycle_ctx.assert_sess.get(SessionModel, sid)
    assert session_row.needs_compression is False

    # 4) messages 顺序断言：[main_system, new_summary, protected_human]
    assert len(captured) == 1
    msgs = captured[0]["messages"]
    assert len(msgs) == 3
    assert isinstance(msgs[0], SystemMessage)
    assert msgs[0].content  # non-empty system prompt
    assert isinstance(msgs[1], SystemMessage)
    assert "新合并摘要" in msgs[1].content
    assert isinstance(msgs[2], HumanMessage)
    assert msgs[2].content == "第三轮"
    assert [type(m).__name__ for m in msgs] == ["SystemMessage", "SystemMessage", "HumanMessage"]


# ---------------------------------------------------------------------------
# A4 e2e: enqueue_audit target_message_id == SSE aid
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_audit_target_message_id_equals_aid(
    app_with_eval, api_client_with_eval, auth_headers_child,
):
    """Given normal graph stream, When chat_stream ends, Then enqueue_audit 的 target_message_id 与 SSE end frame 的 aid 一致。

    独立获取途径：aid 从 SSE end 帧解析（enqueue_audit 外部），
    target_message_id 通过 spy 捕获 enqueue_audit 第 5 位位置参数。
    两者来源不同，assert 为"独立获取一致"，非"自己塞给自己"。
    """
    from unittest.mock import AsyncMock
    from unittest.mock import patch as _patch

    headers, child = auth_headers_child

    fake_payloads = [
        {"delta": "回复内容"},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for p in fake_payloads:
            yield p

    app_with_eval.state.resources.main_graph.astream = fake_astream

    enqueue_spy = AsyncMock()
    with _patch("app.domain.chat.pipeline.enqueue_audit", enqueue_spy):
        body = make_payload(content="你好")
        resp = await api_client_with_eval.post(
            "/api/v1/me/chat/stream", json=body, headers=headers,
        )
        assert resp.status_code == 200
        await resp.aclose()

    frames = _parse_sse_frames(resp.text)
    aid = next(
        f["data"]["aid"] for f in frames
        if f["type"] == "end" and f["data"].get("aid") is not None
    )
    assert aid is not None, "SSE end frame must contain aid"

    # enqueue_audit(arq_pool, audit_redis, sid, db, _turn_number, current.id, aid)
    # target_message_id is positional arg #6 (0-indexed) / #7 (1-indexed)
    call_args = enqueue_spy.call_args[0]
    assert len(call_args) >= 7, f"Expected 7 positional args, got {len(call_args)}"
    str_target = str(call_args[6])  # target_message_id
    assert str_target == aid, (
        f"enqueue_audit target_message_id({str_target}) != SSE aid({aid})"
    )
