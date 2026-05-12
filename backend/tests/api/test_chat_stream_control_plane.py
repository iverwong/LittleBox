"""tests for POST /me/chat/stream control plane (Step 8a).

Decision matrix O (baseline §5.4, 7 rows):
    Row 1: last=None   + regen=null  → INSERT human (active) [session resolved via policy]
    Row 2: last=None   + regen=!null → 400 RegenerateForInvalid
    Row 3: last=AI     + regen=null  → INSERT human (active)
    Row 4: last=AI     + regen=!null → 400 RegenerateForInvalid
    Row 5: last=orphan + regen=null  → UPDATE old discarded + INSERT human (active)
    Row 6: last=orphan + regen=hid   → reuse orphan (no new row, content must be "")
    Row 7: last=orphan + regen=!hid → 400 RegenerateForInvalid

Gate A closing argument (applies to rows 5-7):
    "Last active message" = SELECT ... WHERE status='active'
    ORDER BY created_at DESC, id DESC LIMIT 1 — it is always the latest active row.
    A "non-orphan human" would require an active AI row strictly after it,
    which would itself be the latest active row — contradicting the definition.
    Therefore "last active row is human" ⟺ "orphan human"; no second query needed.
    Rows 8/9 (non-orphan human paths) are unreachable by this argument.

Covers: decision-O 7 rows · throttle lock · session lock · title grapheme · lock release.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import AsyncClient
from sqlalchemy import select

from app.auth.redis_ops import commit_with_redis
from app.auth.tokens import issue_token
from app.chat.graph import main_graph
from app.chat.locks import acquire_session_lock
from app.db import get_db
from app.models.accounts import Family, FamilyMember, User
from app.models.chat import Message
from app.models.chat import Session as SessionModel
from app.models.enums import MessageRole, MessageStatus, UserRole

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def redis_client_with_eval(redis_client: FakeRedis) -> FakeRedis:
    """Patch FakeRedis.eval to simulate Lua DEL-if-nonce-match (fakeredis has no EVAL support)."""
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
    """App fixture using redis_client_with_eval (needed for Lua DEL via release_session_lock)."""
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
    """Async client bound to app_with_eval (uses patched redis_client)."""
    from httpx import ASGITransport

    transport = ASGITransport(app=app_with_eval)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
async def child_user(db_session):
    """Child + family (no ChildProfile — child_profile loading is deferred to 8b)."""
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
    device_id = "test-device-8a"
    token = await issue_token(
        db_session,
        user_id=child_user.id,
        role=UserRole.child,
        family_id=child_user.family_id,
        device_id=device_id,
        ttl_days=None,  # child tokens never expire
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
    content: str = "hello", session_id: str | None = None, regenerate_for: str | None = None
) -> dict:
    """Build a minimal JSON body for POST /me/chat/stream."""
    body: dict = {"content": content}
    if session_id is not None:
        body["session_id"] = session_id
    if regenerate_for is not None:
        body["regenerate_for"] = regenerate_for
    return body


def _make_fake_graph_astream(fake_payloads: list[dict]):
    """Return an async generator that yields fake graph payloads for stream mocking.

    The real main_graph.astream() yields many delta chunks (17+ for a typical reply),
    which breaks hardcoded frame-index assertions like ``frames[2]["type"] == "end"``.
    This fake yields exactly the payloads needed to produce a deterministic 3-frame
    SSE sequence: session_meta → delta → end.
    """

    async def fake_astream(initial_state, stream_mode="custom"):
        for p in fake_payloads:
            yield p

    return fake_astream


def _mock_persist_ai_turn(db, sid, finish_reason, content, intervention_type=None):
    """No-op persist_ai_turn that returns a fake UUID without writing any rows.

    Used by control-plane tests that only need to verify decision-matrix row creation
    (human row handling) without T5 AI row interference.
    """
    return uuid4()


# ---------------------------------------------------------------------------
# Row 1: last=None + regen=null → INSERT session + INSERT human
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_row1_first_turn(api_client_with_eval, auth_headers_child, db_session):
    """Row 1: first turn (session resolved via policy) creates human active, returns 200."""
    headers, child = auth_headers_child
    body = make_payload(content="Hello world")

    # Fake graph yields 1 delta → SSE: session_meta, delta, end (frames[2]=end)
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    with patch.object(main_graph, "astream", fake_astream):
        with patch(
            "app.api.me.persist_ai_turn",
            new_callable=AsyncMock,
            side_effect=_mock_persist_ai_turn,
        ):
            resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
            assert resp.status_code == 200

            # SSE frames: session_meta + delta + end
            frames = _parse_sse_stream(resp.text)
            assert frames[0]["type"] == "session_meta"
            sid = frames[0]["data"]["session_id"]
            assert frames[1]["type"] == "delta"
            assert frames[2]["type"] == "end"

            # DB: session active + human active
            session_row = await db_session.get(SessionModel, sid)
            assert session_row is not None
            assert session_row.status == MessageStatus.active
            assert session_row.child_user_id == child.id
            assert "周" in session_row.title and "月" in session_row.title

    msgs = (
        (
            await db_session.execute(
                select(Message).where(Message.session_id == sid).order_by(Message.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs) == 2
    assert msgs[0].role == MessageRole.human
    assert msgs[0].content == "Hello world"
    assert msgs[0].status == MessageStatus.active
    # commit② 内联写入的 ai 消息
    assert msgs[1].role == MessageRole.ai


# ---------------------------------------------------------------------------
# Row 2: last=None + regen=!null → 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_row2_regen_on_empty_session(api_client_with_eval, auth_headers_child):
    """Row 2: no messages yet but regenerate_for set → 400 RegenerateForInvalid."""
    headers, _ = auth_headers_child
    fake_hid = str(uuid4())
    body = make_payload(content="hello", regenerate_for=fake_hid)

    resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
    assert resp.status_code == 400
    assert "RegenerateForInvalid" in resp.text


# ---------------------------------------------------------------------------
# Row 3: last=AI + regen=null → INSERT human
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_row3_ai_continuation(api_client_with_eval, auth_headers_child, db_session):
    """Row 3: last message is AI, insert new human."""
    headers, child = auth_headers_child

    # Pre-seed session with AI message
    sid = uuid4()
    session = SessionModel(
        id=sid, child_user_id=child.id, title="test", status=MessageStatus.active
    )
    db_session.add(session)
    ai_msg = Message(
        session_id=sid, role=MessageRole.ai, status=MessageStatus.active, content="Hello AI"
    )
    db_session.add(ai_msg)
    await db_session.commit()

    body = make_payload(content="Child reply", session_id=str(sid))
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    with patch.object(main_graph, "astream", fake_astream):
        with patch(
            "app.api.me.persist_ai_turn",
            new_callable=AsyncMock,
            side_effect=_mock_persist_ai_turn,
        ):
            resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
            assert resp.status_code == 200

    msgs = (
        (
            await db_session.execute(
                select(Message)
                .where(Message.session_id == sid, Message.status == MessageStatus.active)
                .order_by(Message.created_at, Message.id)
            )
        )
        .scalars()
        .all()
    )
    # AI (earlier) + new human (later) + commit② AI = 3 active messages
    assert len(msgs) == 3, (
        f"Expected 3 messages, got {len(msgs)}: {[(m.role, m.content[:20]) for m in msgs]}"
    )
    human_msgs = [m for m in msgs if m.role == MessageRole.human]
    assert len(human_msgs) == 1
    assert human_msgs[0].content == "Child reply"


# ---------------------------------------------------------------------------
# Row 4: last=AI + regen=!null → 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_row4_ai_regen_invalid(api_client_with_eval, auth_headers_child, db_session):
    """Row 4: last message is AI, regen set → 400."""
    headers, child = auth_headers_child
    sid = uuid4()

    db_session.add(
        SessionModel(id=sid, child_user_id=child.id, title="test", status=MessageStatus.active)
    )
    ai_msg = Message(
        session_id=sid, role=MessageRole.ai, status=MessageStatus.active, content="AI msg"
    )
    db_session.add(ai_msg)
    await db_session.flush()
    await db_session.commit()

    body = make_payload(content="hello", session_id=str(sid), regenerate_for=str(ai_msg.id))
    resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
    assert resp.status_code == 400
    assert "RegenerateForInvalid" in resp.text


# ---------------------------------------------------------------------------
# Row 5: orphan + regen=null → UPDATE old discarded + INSERT human
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_row5_orphan_regen_null(
    api_client_with_eval, auth_headers_child, db_session
):
    """Row 5: orphan human + null → UPDATE old discarded + INSERT new human; both in same tx."""
    headers, child = auth_headers_child
    sid = uuid4()

    db_session.add(
        SessionModel(id=sid, child_user_id=child.id, title="test", status=MessageStatus.active)
    )
    # Orphan = human, no subsequent AI
    orphan = Message(
        session_id=sid, role=MessageRole.human, status=MessageStatus.active, content="Old content"
    )
    db_session.add(orphan)
    await db_session.flush()
    orphan_id = orphan.id
    await db_session.commit()

    body = make_payload(content="New content", session_id=str(sid))
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    with patch.object(main_graph, "astream", fake_astream):
        with patch(
            "app.api.me.persist_ai_turn",
            new_callable=AsyncMock,
            side_effect=_mock_persist_ai_turn,
        ):
            resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
            assert resp.status_code == 200

    msgs = (
        (
            await db_session.execute(
                select(Message)
                .where(Message.session_id == sid)
                .order_by(Message.created_at, Message.id)
            )
        )
        .scalars()
        .all()
    )
    # Old discarded + new human active + commit② AI = 3 rows total
    assert len(msgs) == 3
    discarded = [m for m in msgs if m.status == MessageStatus.discarded]
    active = [m for m in msgs if m.status == MessageStatus.active]
    assert len(discarded) == 1, f"Expected 1 discarded, got {[(m.id, m.status) for m in msgs]}"
    assert discarded[0].id == orphan_id
    assert len(active) == 2, f"Expected 2 active (human + ai), got {len(active)}"
    assert active[0].role == MessageRole.human
    assert active[0].content == "New content"
    assert active[1].role == MessageRole.ai


# ---------------------------------------------------------------------------
# Row 6: orphan + regen=hid → reuse orphan, no new row, content must be ""
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_row6_orphan_reuse(
    api_client_with_eval, auth_headers_child, db_session
):
    """Row 6: orphan + =hid → reuse orphan row (no INSERT, no content update)."""
    headers, child = auth_headers_child
    sid = uuid4()

    db_session.add(
        SessionModel(id=sid, child_user_id=child.id, title="test", status=MessageStatus.active)
    )
    orphan = Message(
        session_id=sid,
        role=MessageRole.human,
        status=MessageStatus.active,
        content="Original question",
    )
    db_session.add(orphan)
    await db_session.flush()
    orphan_id = orphan.id
    await db_session.commit()

    # Row 6: content should be "" (Option A — strict contract)
    body = make_payload(content="", session_id=str(sid), regenerate_for=str(orphan_id))
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    with patch.object(main_graph, "astream", fake_astream):
        with patch(
            "app.api.me.persist_ai_turn",
            new_callable=AsyncMock,
            side_effect=_mock_persist_ai_turn,
        ):
            resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
            assert resp.status_code == 200

    frames = _parse_sse_stream(resp.text)
    hid_in_meta = frames[0]["data"]["hid"]
    assert hid_in_meta == str(orphan_id)  # hid unchanged

    msgs = (
        (await db_session.execute(select(Message).where(Message.session_id == sid))).scalars().all()
    )
    # 复用 orphan + commit② AI = 2 rows
    assert len(msgs) == 2
    assert msgs[0].id == orphan_id
    assert msgs[0].content == "Original question"  # content unchanged
    assert msgs[1].role == MessageRole.ai


@pytest.mark.asyncio
async def test_decision_row6_content_must_be_empty(
    api_client_with_eval, auth_headers_child, db_session
):
    """Row 6: orphan + =hid but content non-empty → 400 (strict contract Option A)."""
    headers, child = auth_headers_child
    sid = uuid4()

    db_session.add(
        SessionModel(id=sid, child_user_id=child.id, title="test", status=MessageStatus.active)
    )
    orphan = Message(
        session_id=sid, role=MessageRole.human, status=MessageStatus.active, content="Q"
    )
    db_session.add(orphan)
    await db_session.flush()
    await db_session.commit()

    # Row 6: content="" enforced — non-empty → 400
    body = make_payload(content="IGNORED", session_id=str(sid), regenerate_for=str(orphan.id))
    resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Row 7: orphan + regen=!hid → 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_row7_orphan_history_regen(
    api_client_with_eval, auth_headers_child, db_session
):
    """Row 7: orphan + regenerate_for pointing to a different (earlier) message → 400."""
    headers, child = auth_headers_child
    sid = uuid4()

    db_session.add(
        SessionModel(id=sid, child_user_id=child.id, title="test", status=MessageStatus.active)
    )
    # First message (earlier)
    earlier = Message(
        session_id=sid, role=MessageRole.human, status=MessageStatus.active, content="Earlier"
    )
    db_session.add(earlier)
    await db_session.flush()
    # Orphan (last)
    orphan = Message(
        session_id=sid, role=MessageRole.human, status=MessageStatus.active, content="Orphan"
    )
    db_session.add(orphan)
    await db_session.flush()
    await db_session.commit()

    # Try to regenerate the earlier message (not the orphan)
    body = make_payload(content="ignored", session_id=str(sid), regenerate_for=str(earlier.id))
    resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
    assert resp.status_code == 400
    assert "RegenerateForInvalid" in resp.text


# ---------------------------------------------------------------------------
# Row 8: not orphan + regen=null → INSERT human
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_row3_with_prior_human(
    api_client_with_eval, auth_headers_child, db_session
):
    """Row 3 sub-scenario: session has H1 + AI already, insert new human (H2).

    Covers H1 (active) + A1 (active) + new human → 3 active rows.
    This is the "non-orphan continuation" path: last_msg = AI, so we INSERT human.
    """
    headers, child = auth_headers_child
    sid = uuid4()

    db_session.add(
        SessionModel(id=sid, child_user_id=child.id, title="test", status=MessageStatus.active)
    )
    base = datetime.now(UTC)
    human = Message(
        session_id=sid, role=MessageRole.human, status=MessageStatus.active,
        content="H1", created_at=base,
    )
    ai = Message(
        session_id=sid, role=MessageRole.ai, status=MessageStatus.active,
        content="AI reply", created_at=base + timedelta(milliseconds=1),
    )
    db_session.add_all([human, ai])
    await db_session.flush()
    human_id = human.id
    await db_session.flush()

    body = make_payload(content="H2 continuation", session_id=str(sid))
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    with patch.object(main_graph, "astream", fake_astream):
        with patch(
            "app.api.me.persist_ai_turn",
            new_callable=AsyncMock,
            side_effect=_mock_persist_ai_turn,
        ):
            resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
            assert resp.status_code == 200

    msgs = (
        (
            await db_session.execute(
                select(Message)
                .where(Message.session_id == sid, Message.status == MessageStatus.active)
                .order_by(Message.created_at, Message.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs) == 4  # H1 + AI + H2 + commit② AI
    human_msgs = [m for m in msgs if m.role == MessageRole.human]
    assert len(human_msgs) == 2
    new_human = next(m for m in human_msgs if m.id != human_id)
    assert new_human.content == "H2 continuation"


# ---------------------------------------------------------------------------
# Row 5 (with prior AI): orphan + regen=null → UPDATE old discarded + INSERT human
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_row5_with_prior_ai(
    api_client_with_eval, auth_headers_child, db_session
):
    """Row 5: H1 + A1 + H2 active, regen=null → UPDATE H2 discarded + INSERT H3 active.

    PG timestamp construction: explicit created_at ensures H2 is the confirmed last
    active row by ORDER BY created_at DESC, id DESC.
    """
    headers, child = auth_headers_child
    sid = uuid4()

    db_session.add(
        SessionModel(id=sid, child_user_id=child.id, title="test", status=MessageStatus.active)
    )
    base = datetime.now(UTC)
    h1 = Message(
        session_id=sid, role=MessageRole.human, status=MessageStatus.active,
        content="H1", created_at=base,
    )
    a1 = Message(
        session_id=sid, role=MessageRole.ai, status=MessageStatus.active,
        content="A1", created_at=base + timedelta(milliseconds=1),
    )
    h2 = Message(
        session_id=sid, role=MessageRole.human, status=MessageStatus.active,
        content="H2", created_at=base + timedelta(milliseconds=2),
    )
    db_session.add_all([h1, a1, h2])
    await db_session.flush()
    h2_id = h2.id
    await db_session.flush()

    body = make_payload(content="H3 content", session_id=str(sid))
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    with patch.object(main_graph, "astream", fake_astream):
        with patch(
            "app.api.me.persist_ai_turn",
            new_callable=AsyncMock,
            side_effect=_mock_persist_ai_turn,
        ):
            resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
            assert resp.status_code == 200

    frames = _parse_sse_stream(resp.text)
    hid_in_meta = frames[0]["data"]["hid"]

    msgs = (
        (await db_session.execute(select(Message).where(Message.session_id == sid)
                                  .order_by(Message.created_at, Message.id))).scalars().all()
    )
    assert len(msgs) == 5  # H1 + A1 + H2(discarded) + H3 + commit② AI
    discarded = [m for m in msgs if m.status == MessageStatus.discarded]
    active = [m for m in msgs if m.status == MessageStatus.active]
    assert len(discarded) == 1
    assert discarded[0].id == h2_id
    assert len(active) == 4  # H1 + A1 + H3 + commit② AI
    new_human = next(m for m in active if m.role == MessageRole.human and m.content == "H3 content")
    assert str(new_human.id) == hid_in_meta  # hid is H3, not H2


# ---------------------------------------------------------------------------
# Row 6 (with prior AI): orphan + regen=hid + content="" → reuse orphan
# Setup: H1 + A1 + H2 (3 active), last=H2 human, regen=H2.id, content=""
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_row6_with_prior_ai_reuse(
    api_client_with_eval, auth_headers_child, db_session
):
    """Row 6: H1 + A1 + H2 active, regen=H2.id + content="" → reuse H2 (no new row).

    The Gate A closing argument says "last active row is human" ⟺ "orphan", so H2 is
    the orphan.  Row 6 should reuse it: hid=H2.id, no new row inserted.
    """
    headers, child = auth_headers_child
    sid = uuid4()

    db_session.add(
        SessionModel(id=sid, child_user_id=child.id, title="test", status=MessageStatus.active)
    )
    base = datetime.now(UTC)
    h1 = Message(
        session_id=sid, role=MessageRole.human, status=MessageStatus.active,
        content="H1", created_at=base,
    )
    a1 = Message(
        session_id=sid, role=MessageRole.ai, status=MessageStatus.active,
        content="A1", created_at=base + timedelta(milliseconds=1),
    )
    h2 = Message(
        session_id=sid, role=MessageRole.human, status=MessageStatus.active,
        content="H2 original", created_at=base + timedelta(milliseconds=2),
    )
    db_session.add_all([h1, a1, h2])
    await db_session.flush()
    h2_id = h2.id
    await db_session.flush()

    body = make_payload(content="", session_id=str(sid), regenerate_for=str(h2_id))
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    with patch.object(main_graph, "astream", fake_astream):
        with patch(
            "app.api.me.persist_ai_turn",
            new_callable=AsyncMock,
            side_effect=_mock_persist_ai_turn,
        ):
            resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
            assert resp.status_code == 200

    frames = _parse_sse_stream(resp.text)
    assert frames[0]["data"]["hid"] == str(h2_id)  # hid unchanged

    msgs = (
        (await db_session.execute(select(Message).where(Message.session_id == sid)
                                  .order_by(Message.created_at, Message.id))).scalars().all()
    )
    assert len(msgs) == 4  # H1 + A1 + H2 + commit② AI
    h2_row = next(m for m in msgs if m.id == h2_id)
    assert h2_row.content == "H2 original"  # content unchanged
    assert h2_row.status == MessageStatus.active


# ---------------------------------------------------------------------------
# Throttle lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_throttle_lock_rejects_second_request(api_client_with_eval, auth_headers_child):
    """1 s 内连发两次 → 第二次 429 RequestThrottled (throttle TTL 自然过期)."""
    headers, _ = auth_headers_child
    body = make_payload(content="first")
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    with patch.object(main_graph, "astream", fake_astream):
        with patch(
            "app.api.me.persist_ai_turn",
            new_callable=AsyncMock,
            side_effect=_mock_persist_ai_turn,
        ):
            resp1 = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
            assert resp1.status_code == 200

            # Immediately send second request (within 1.5s TTL)
            body2 = make_payload(content="second")
            resp2 = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body2, headers=headers
            )
            assert resp2.status_code == 429
            assert "RequestThrottled" in resp2.text


# ---------------------------------------------------------------------------
# Session lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_lock_rejects_concurrent(
    api_client_with_eval, auth_headers_child, redis_client, db_session
):
    """同 sid 锁未释放时第二次请求 → 409 SessionBusy."""
    headers, child = auth_headers_child
    sid = uuid4()

    # Create a minimal session directly in DB
    session = SessionModel(
        id=sid, child_user_id=child.id, title="locked", status=MessageStatus.active
    )
    db_session.add(session)
    await db_session.commit()

    # Manually acquire the session lock (simulate an in-flight request)
    nonce = await acquire_session_lock(redis_client, str(sid))
    assert nonce is not None

    # Second request with same session_id should get 409
    body = make_payload(content="second request", session_id=str(sid))
    resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
    assert resp.status_code == 409
    assert "SessionBusy" in resp.text

    # Clean up lock
    from app.chat.locks import release_session_lock

    await release_session_lock(redis_client, str(sid), nonce)


# ---------------------------------------------------------------------------
# 404 / 403: session not found / child mismatch — no lock acquired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_400_releases_session_lock(
    api_client_with_eval, auth_headers_child, redis_client, db_session
):
    """HTTPException 400 before StreamingResponse → session lock explicitly released."""
    headers, child = auth_headers_child
    sid = uuid4()

    # Pre-seed session
    db_session.add(
        SessionModel(id=sid, child_user_id=child.id, title="test", status=MessageStatus.active)
    )
    ai_msg = Message(session_id=sid, role=MessageRole.ai, status=MessageStatus.active, content="AI")
    db_session.add(ai_msg)
    await db_session.flush()
    await db_session.commit()

    # Row 4: AI + regen set → 400. Lock must be released.
    body = make_payload(content="hello", session_id=str(sid), regenerate_for=str(ai_msg.id))
    resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
    assert resp.status_code == 400

    # Simulate throttle TTL expiry by manually deleting the throttle key
    throttle_key = f"chat:throttle:{child.id}"
    await redis_client.delete(throttle_key)

    # Verify lock was released — a new request with same sid should succeed
    body2 = make_payload(content="hello again", session_id=str(sid))
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    with patch.object(main_graph, "astream", fake_astream):
        resp2 = await api_client_with_eval.post(
            "/api/v1/me/chat/stream", json=body2, headers=headers
        )
        assert resp2.status_code == 200


@pytest.mark.asyncio
async def test_throttle_lock_self_expires(
    api_client_with_eval, auth_headers_child, redis_client
):
    """Throttle key uses TTL (SETNX px=1500), not actively deleted."""
    headers, child = auth_headers_child
    body = make_payload(content="first")
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    with patch.object(main_graph, "astream", fake_astream):
        with patch(
            "app.api.me.persist_ai_turn",
            new_callable=AsyncMock,
            side_effect=_mock_persist_ai_turn,
        ):
            resp1 = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
            assert resp1.status_code == 200

    # Key exists with TTL ~1500ms
    throttle_key = f"chat:throttle:{child.id}"
    ttl = await redis_client.pttl(throttle_key)
    assert 0 < ttl <= 1500, f"Expected 0 < TTL <= 1500, got {ttl}"


# ---------------------------------------------------------------------------
# Title grapheme truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_title_12_graphemes_ascii(api_client_with_eval, auth_headers_child, db_session):
    """Unicode TR29 grapheme cluster: 12-grapheme title truncation (ZWJ = 1 cluster)."""
    headers, _ = auth_headers_child
    body = make_payload(content="Hello 你好 👨‍👩‍👧 abcdef")
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    with patch.object(main_graph, "astream", fake_astream):
        with patch(
            "app.api.me.persist_ai_turn",
            new_callable=AsyncMock,
            side_effect=_mock_persist_ai_turn,
        ):
            resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
            assert resp.status_code == 200

            sid = _parse_sse_stream(resp.text)[0]["data"]["session_id"]
    session = await db_session.get(SessionModel, sid)
    # M6-patch3 Step 6: session title = today_session_title() 中文日期格式
    assert session.title is not None
    assert "周" in session.title and "月" in session.title and "日" in session.title


@pytest.mark.asyncio
async def test_title_zwj_emoji_counts_as_one_grapheme(
    api_client_with_eval, auth_headers_child, db_session
):
    """TR29 ZWJ emoji family = 1 grapheme cluster (not 3), anchored to prevent regression."""
    headers, _ = auth_headers_child
    body = make_payload(content="👨‍👩‍👧‍👦‍👧")  # 6-person family ZWJ sequence
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    with patch.object(main_graph, "astream", fake_astream):
        with patch(
            "app.api.me.persist_ai_turn",
            new_callable=AsyncMock,
            side_effect=_mock_persist_ai_turn,
        ):
            resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
            assert resp.status_code == 200

            sid = _parse_sse_stream(resp.text)[0]["data"]["session_id"]
    session = await db_session.get(SessionModel, sid)
    # M6-patch3 Step 6: session title = today_session_title() 中文日期格式
    assert session.title is not None
    assert "周" in session.title and "月" in session.title and "日" in session.title


# ---------------------------------------------------------------------------
# Lock finally releases correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_released_in_generator_finally(
    api_client_with_eval, auth_headers_child, redis_client
):
    """Successful request → session lock released in generator finally."""
    headers, _ = auth_headers_child
    body = make_payload(content="hello")
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    with patch.object(main_graph, "astream", fake_astream):
        with patch(
            "app.api.me.persist_ai_turn",
            new_callable=AsyncMock,
            side_effect=_mock_persist_ai_turn,
        ):
            resp = await api_client_with_eval.post(
                "/api/v1/me/chat/stream", json=body, headers=headers
            )
            assert resp.status_code == 200
            await resp.aclose()  # ensure response fully consumed

    # After response closes, lock should be gone
    from app.chat.locks import acquire_session_lock

    nonce = await acquire_session_lock(
        redis_client, _parse_sse_stream(resp.text)[0]["data"]["session_id"]
    )
    assert nonce is not None  # lock was released and we can re-acquire


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _parse_sse_stream(raw: str) -> list[dict]:
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
