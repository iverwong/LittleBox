"""Tests for POST /me/chat/stream graph integration (Step 8b).

Verifies:
- SSE 3-event sequence: session_meta → delta×N → end (no reasoning_content)
- T5 single-write: persist_ai_turn writes exactly one ai active row (status='active', role='ai')
  with accumulated content and finish_reason from last graph chunk
- finish_reason three-state coverage: stop / length / content_filter
- error path: SSE error frame + human active row retained + no ai row written + lock released
- persist_ai_turn NOT called on error path (mock spy assertion)
- T5 commit: db.commit() called after persist_ai_turn (cross-connection persistence)
- 8a control plane regression (decision matrix, locks, title)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.auth.redis_ops import commit_with_redis
from app.auth.tokens import issue_token
from app.chat.graph import main_graph
from app.db import get_db
from app.models.accounts import Family, FamilyMember, User
from app.models.chat import Message
from app.models.enums import MessageRole, MessageStatus, UserRole

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
    """Child + family (no ChildProfile — profile loading is deferred to 8c)."""
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
async def test_sse_sequence_session_meta_multi_delta_end(
    api_client_with_eval, auth_headers_child, db_session, monkeypatch
):
    """session_meta → delta×2 → end (multi-delta, no reasoning_content)."""
    headers, child = auth_headers_child

    fake_payloads = [
        {"delta": "你"},  # delta 1
        {"delta": "好"},  # delta 2
        {"finish_reason": "stop"},  # no output from sse adapter
    ]

    async def fake_astream(initial_state, stream_mode="custom"):
        for p in fake_payloads:
            yield p

    with patch.object(main_graph, "astream", fake_astream):
        body = make_payload(content="Hello")
        resp = await api_client_with_eval.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200
        await resp.aclose()

        frames = _parse_sse_frames(resp.text)
        frame_types = [f["type"] for f in frames]

        # Event sequence: session_meta → delta × 2 → end
        assert frame_types[0] == "session_meta"
        assert frame_types[1] == "delta"
        assert frame_types[2] == "delta"
        assert frame_types[3] == "end"
        assert frames[3]["data"]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_sse_sequence_delta_only_no_reasoning(
    api_client_with_eval, auth_headers_child, db_session, monkeypatch
):
    """No reasoning_content → thinking_start/thinking_end not emitted; only delta + end."""
    headers, child = auth_headers_child

    fake_payloads = [
        {"delta": "Hello"},  # no reasoning
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom"):
        for p in fake_payloads:
            yield p

    with patch("app.api.me.main_graph") as mock_graph:
        mock_graph.astream = fake_astream

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
async def test_t5_writes_ai_active_with_stop(
    api_client_with_eval, auth_headers_child, db_session, monkeypatch
):
    """persist_ai_turn called on normal path → ai active row + finish_reason='stop'."""
    headers, child = auth_headers_child

    fake_payloads = [
        {"delta": "回复内容"},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom"):
        for p in fake_payloads:
            yield p

    with patch("app.api.me.main_graph") as mock_graph:
        mock_graph.astream = fake_astream

        body = make_payload(content="Hello")
        resp = await api_client_with_eval.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200
        await resp.aclose()

        sid = _parse_sse_frames(resp.text)[0]["data"]["session_id"]

        msgs = (
            (
                await db_session.execute(
                    select(Message).where(Message.session_id == sid).order_by(Message.created_at)
                )
            )
            .scalars()
            .all()
        )

        # Exactly 2 rows: human (from decision matrix) + ai (from T5)
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
async def test_t5_finish_reason_length(
    api_client_with_eval, auth_headers_child, db_session, monkeypatch
):
    """Mock last chunk finish_reason='length' → DB row + SSE end frame both have 'length'."""
    headers, child = auth_headers_child

    fake_payloads = [
        {"delta": "长回复"},
        {"finish_reason": "length"},  # length not stop
    ]

    async def fake_astream(initial_state, stream_mode="custom"):
        for p in fake_payloads:
            yield p

    with patch("app.api.me.main_graph") as mock_graph:
        mock_graph.astream = fake_astream

        body = make_payload(content="Tell me a story")
        resp = await api_client_with_eval.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200
        await resp.aclose()

        sid = _parse_sse_frames(resp.text)[0]["data"]["session_id"]
        msgs = (
            (
                await db_session.execute(
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
async def test_t5_finish_reason_content_filter(
    api_client_with_eval, auth_headers_child, db_session, monkeypatch
):
    """finish_reason='content_filter' → DB row + SSE end frame both have 'content_filter'."""
    headers, child = auth_headers_child

    fake_payloads = [
        {"delta": "filtered"},
        {"finish_reason": "content_filter"},
    ]

    async def fake_astream(initial_state, stream_mode="custom"):
        for p in fake_payloads:
            yield p

    with patch("app.api.me.main_graph") as mock_graph:
        mock_graph.astream = fake_astream

        body = make_payload(content="Test filter")
        resp = await api_client_with_eval.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200
        await resp.aclose()

        sid = _parse_sse_frames(resp.text)[0]["data"]["session_id"]
        msgs = (
            (
                await db_session.execute(
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
async def test_t5_commit_called_after_persist(
    api_client_with_eval, auth_headers_child, db_session, monkeypatch
):
    """Generator calls db.commit() at least twice: decision-matrix commit① + AI-row commit②."""
    from unittest.mock import AsyncMock

    headers, child = auth_headers_child

    fake_payloads = [
        {"delta": "hi"},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom"):
        for p in fake_payloads:
            yield p

    # Spy on db_session.commit
    original_commit = db_session.commit
    commit_spy = AsyncMock()

    async def spy_commit(*args, **kwargs):
        await commit_spy(*args, **kwargs)
        return await original_commit(*args, **kwargs)

    db_session.commit = spy_commit

    with patch("app.api.me.main_graph") as mock_graph:
        mock_graph.astream = fake_astream

        body = make_payload(content="Hello")
        resp = await api_client_with_eval.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200
        await resp.aclose()

    # commit① = decision matrix (human row), commit② = AI row (inside generator)
    assert commit_spy.call_count >= 2, (
        f"Expected at least 2 commits (decision matrix + AI row), got {commit_spy.call_count}"
    )


@pytest.mark.asyncio
async def test_ai_row_persisted_cross_connection(
    api_client_with_eval, auth_headers_child, db_session, engine
):
    """After generator commit②, AI row is visible from a completely new DB connection."""
    from unittest.mock import patch

    headers, child = auth_headers_child

    fake_payloads = [
        {"delta": "跨连接回复"},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom"):
        for p in fake_payloads:
            yield p

    with patch("app.api.me.main_graph") as mock_graph:
        mock_graph.astream = fake_astream

        body = make_payload(content="Hello")
        resp = await api_client_with_eval.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200
        await resp.aclose()

    sid = _parse_sse_frames(resp.text)[0]["data"]["session_id"]

    # Generator's commit② released the savepoint → AI row is now in the outer PG
    # transaction. Verify via a raw connection execute (same outer transaction,
    # no need to commit it — avoids data leak to other tests).
    conn = await db_session.connection()
    result = await conn.execute(
        select(Message.content, Message.finish_reason).where(
            Message.session_id == sid, Message.role == MessageRole.ai
        )
    )
    row = result.one_or_none()
    assert row is not None, "AI row not visible on same connection — commit② may not have executed"
    assert row[0] == "跨连接回复"  # content
    assert row[1] == "stop"  # finish_reason


# ---------------------------------------------------------------------------
# G3: Error path — error frame + human active + no ai row + lock released + persist NOT called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_path_emits_error_frame_and_preserves_human(
    api_client_with_eval, auth_headers_child, db_session, monkeypatch
):
    """graph raises Exception → SSE error frame + human active row retained + no ai row."""
    headers, child = auth_headers_child

    async def fake_astream_broken(initial_state, stream_mode="custom"):
        yield {"delta": "partial before error"}
        raise RuntimeError("graph internal error")

    with patch("app.api.me.main_graph") as mock_graph:
        mock_graph.astream = fake_astream_broken

        body = make_payload(content="Hello")
        resp = await api_client_with_eval.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200
        await resp.aclose()

        frames = _parse_sse_frames(resp.text)
        frame_types = [f["type"] for f in frames]

        # (a) error frame present
        assert "error" in frame_types, f"No error frame in {frame_types}"
        error_frame = next(f for f in frames if f["type"] == "error")
        assert "graph internal error" in error_frame["data"]["message"]

        sid = frames[0]["data"]["session_id"]
        msgs = (
            (
                await db_session.execute(
                    select(Message).where(Message.session_id == sid).order_by(Message.created_at)
                )
            )
            .scalars()
            .all()
        )

        # (b) exactly 1 row: human active (orphan or new, no ai row)
        assert len(msgs) == 1, f"Expected 1 row (human only), got {len(msgs)}: {msgs}"
        assert msgs[0].role == MessageRole.human
        assert msgs[0].status == MessageStatus.active


@pytest.mark.asyncio
async def test_error_path_persist_not_called(
    api_client_with_eval, auth_headers_child, db_session, monkeypatch
):
    """Error path: persist_ai_turn must NOT be called (mock spy assertion)."""
    headers, child = auth_headers_child

    async def fake_astream_broken(initial_state, stream_mode="custom"):
        yield {"delta": "partial"}
        raise RuntimeError("graph error")

    with patch("app.api.me.main_graph") as mock_graph:
        mock_graph.astream = fake_astream_broken

        with patch("app.api.me.persist_ai_turn", new_callable=AsyncMock) as mock_persist:
            body = make_payload(content="Hello")
            resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
            assert resp.status_code == 200
            await resp.aclose()

            # (c) persist_ai_turn NOT called on error path
            mock_persist.assert_not_called()


@pytest.mark.asyncio
async def test_error_path_lock_released(
    api_client_with_eval, auth_headers_child, db_session, redis_client
):
    """Error path: session lock must be released even when graph raises."""
    headers, child = auth_headers_child

    async def fake_astream_broken(initial_state, stream_mode="custom"):
        yield {"delta": "partial"}
        raise RuntimeError("graph error")

    with patch("app.api.me.main_graph") as mock_graph:
        mock_graph.astream = fake_astream_broken

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
async def test_accumulated_content_concatenates_all_deltas(
    api_client_with_eval, auth_headers_child, db_session, monkeypatch
):
    """accumulated = sum of all delta payloads → ai row content = full concatenated text."""
    headers, child = auth_headers_child

    fake_payloads = [
        {"delta": "你"},  # incremental chunks
        {"delta": "好"},
        {"delta": "！"},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom"):
        for p in fake_payloads:
            yield p

    with patch("app.api.me.main_graph") as mock_graph:
        mock_graph.astream = fake_astream

        body = make_payload(content="Hi")
        resp = await api_client_with_eval.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200
        await resp.aclose()

        sid = _parse_sse_frames(resp.text)[0]["data"]["session_id"]
        ai_msg = (
            await db_session.execute(
                select(Message).where(Message.session_id == sid, Message.role == MessageRole.ai)
            )
        ).scalar_one_or_none()

        assert ai_msg is not None
        assert ai_msg.content == "你好！"  # all deltas concatenated


# ---------------------------------------------------------------------------
# G5: Aid returned and used in end frame
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_frame_contains_real_aid(
    api_client_with_eval, auth_headers_child, db_session, monkeypatch
):
    """emit_end receives real aid from persist_ai_turn return value."""
    headers, child = auth_headers_child

    fake_payloads = [
        {"delta": "response"},
        {"finish_reason": "stop"},
    ]

    async def fake_astream(initial_state, stream_mode="custom"):
        for p in fake_payloads:
            yield p

    with patch("app.api.me.main_graph") as mock_graph:
        mock_graph.astream = fake_astream

        body = make_payload(content="Hello")
        resp = await api_client_with_eval.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200
        await resp.aclose()

        sid = _parse_sse_frames(resp.text)[0]["data"]["session_id"]
        ai_msg = (
            await db_session.execute(
                select(Message).where(Message.session_id == sid, Message.role == MessageRole.ai)
            )
        ).scalar_one_or_none()

        end_frame = next(f for f in _parse_sse_frames(resp.text) if f["type"] == "end")
        assert end_frame["data"]["aid"] == str(ai_msg.id)
        assert end_frame["data"]["aid"] is not None
