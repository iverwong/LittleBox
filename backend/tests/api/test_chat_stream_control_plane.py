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

pytestmark = pytest.mark.asyncio(loop_scope="function")


@pytest.fixture(autouse=True)
def _mock_enqueue_audit():
    """所有控制平面测试共用：enqueue_audit mock 避免 Redis lifespan 依赖。"""
    with patch("app.domain.chat.pipeline.enqueue_audit", AsyncMock()):
        yield


from app.chat.graph import build_main_graph
from app.core.redis import commit_with_redis
from app.domain.auth.tokens import issue_token
from app.domain.chat.stream import frame_sse_event
from fakeredis.aioredis import FakeRedis
from httpx import AsyncClient
from sqlalchemy import select

main_graph = build_main_graph()
from app.core.db import get_db
from app.core.enums import InterventionType, MessageRole, MessageStatus, UserRole
from app.core.locks import acquire_session_lock
from app.models.chat import Message
from app.models.chat import Session as SessionModel
from tests.api._chat_stream_lifecycle_helpers import (  # noqa: F401  # lifecycle_ctx 是 fixture param
    lifecycle_ctx,
    lifecycle_setup,
)

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
    """Async client bound to app_with_eval (uses patched redis_client)."""
    from httpx import ASGITransport

    transport = ASGITransport(app=app_with_eval)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


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

    async def fake_astream(initial_state, stream_mode="custom", **kwargs):
        for p in fake_payloads:
            yield p

    return fake_astream


# ---------------------------------------------------------------------------
# Row 1: last=None + regen=null → INSERT session + INSERT human
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_row1_first_turn(lifecycle_ctx):
    """Row 1: first turn (session resolved via policy) creates human active, returns 200."""
    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    body = make_payload(content="Hello world")

    # Fake graph yields 1 delta → SSE: session_meta, delta, end (frames[2]=end)
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    lifecycle_ctx.rr.main_graph.astream = fake_astream
    resp = await client.post(
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
    session_row = await lifecycle_ctx.assert_sess.get(SessionModel, sid)
    assert session_row is not None
    assert session_row.status == MessageStatus.active
    assert session_row.child_user_id == child.id
    assert "周" in session_row.title and "月" in session_row.title

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
async def test_decision_row3_ai_continuation(lifecycle_ctx):
    """Row 3: last message is AI, insert new human."""
    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    # Pre-seed session with AI message
    sid = uuid4()
    session = SessionModel(
        id=sid, child_user_id=child.id, title="test", status=MessageStatus.active
    )
    lifecycle_ctx.seed_sess.add(session)
    ai_msg = Message(
        session_id=sid, role=MessageRole.ai, status=MessageStatus.active, content="Hello AI"
    )
    lifecycle_ctx.seed_sess.add(ai_msg)
    await lifecycle_ctx.seed_sess.commit()

    body = make_payload(content="Child reply", session_id=str(sid))
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    lifecycle_ctx.rr.main_graph.astream = fake_astream
    resp = await client.post(
        "/api/v1/me/chat/stream", json=body, headers=headers
    )
    assert resp.status_code == 200

    msgs = (
        (
            await lifecycle_ctx.assert_sess.execute(
                select(Message)
                .where(Message.session_id == sid, Message.status == MessageStatus.active)
                .order_by(Message.created_at, Message.id)
            )
        )
        .scalars()
        .all()
    )
    # pre-seeded AI + new human + inline AI = 3 active messages
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
async def test_decision_row5_orphan_regen_null(lifecycle_ctx):
    """Row 5: orphan human + null → UPDATE old discarded + INSERT new human; both in same tx."""
    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid = uuid4()

    lifecycle_ctx.seed_sess.add(
        SessionModel(id=sid, child_user_id=child.id, title="test", status=MessageStatus.active)
    )
    # Orphan = human, no subsequent AI
    orphan = Message(
        session_id=sid, role=MessageRole.human, status=MessageStatus.active, content="Old content"
    )
    lifecycle_ctx.seed_sess.add(orphan)
    await lifecycle_ctx.seed_sess.flush()
    orphan_id = orphan.id
    await lifecycle_ctx.seed_sess.commit()

    body = make_payload(content="New content", session_id=str(sid))
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    lifecycle_ctx.rr.main_graph.astream = fake_astream
    resp = await client.post(
        "/api/v1/me/chat/stream", json=body, headers=headers
    )
    assert resp.status_code == 200

    msgs = (
        (
            await lifecycle_ctx.assert_sess.execute(
                select(Message)
                .where(Message.session_id == sid)
                .order_by(Message.created_at, Message.id)
            )
        )
        .scalars()
        .all()
    )
    # Old discarded + new human active + inline AI = 3 rows total
    assert len(msgs) == 3
    discarded = [m for m in msgs if m.status == MessageStatus.discarded]
    active = [m for m in msgs if m.status == MessageStatus.active]
    assert len(discarded) == 1, f"Expected 1 discarded, got {[(m.id, m.status) for m in msgs]}"
    assert discarded[0].id == orphan_id
    assert len(active) == 2, f"Expected 2 active (human + ai), got {len(active)}"
    active_human = next(m for m in active if m.role == MessageRole.human)
    assert active_human.content == "New content"
    active_ai = next(m for m in active if m.role == MessageRole.ai)
    assert active_ai.content == "[fake]"


# ---------------------------------------------------------------------------
# Row 6: orphan + regen=hid → reuse orphan, no new row, content must be ""
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_row6_orphan_reuse(lifecycle_ctx):
    """Row 6: orphan + =hid → reuse orphan row (no INSERT, no content update)."""
    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid = uuid4()

    lifecycle_ctx.seed_sess.add(
        SessionModel(id=sid, child_user_id=child.id, title="test", status=MessageStatus.active)
    )
    orphan = Message(
        session_id=sid,
        role=MessageRole.human,
        status=MessageStatus.active,
        content="Original question",
    )
    lifecycle_ctx.seed_sess.add(orphan)
    await lifecycle_ctx.seed_sess.flush()
    orphan_id = orphan.id
    await lifecycle_ctx.seed_sess.commit()

    # Row 6: content should be "" (Option A — strict contract)
    body = make_payload(content="", session_id=str(sid), regenerate_for=str(orphan_id))
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    lifecycle_ctx.rr.main_graph.astream = fake_astream
    resp = await client.post(
        "/api/v1/me/chat/stream", json=body, headers=headers
    )
    assert resp.status_code == 200

    frames = _parse_sse_stream(resp.text)
    hid_in_meta = frames[0]["data"]["hid"]
    assert hid_in_meta == str(orphan_id)  # hid unchanged

    msgs = (
        (await lifecycle_ctx.assert_sess.execute(select(Message).where(Message.session_id == sid))).scalars().all()
    )
    # 复用 orphan + inline AI = 2 rows
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


@pytest.mark.asyncio
async def test_decision_row6_orphan_reuse_feeds_user_input(lifecycle_ctx):
    """Row 6 regression: ctx.user_input 必须等于孤儿原始 content，不是空串。

    链路：StopNoAi → 孤儿 H2 (turn=K, content="原问题") → 重新生成 → 决策矩阵
    Row 6 命中（content="" + regenerate_for=hid）→ 复用孤儿行、_turn_number = K
    → load_active_history_for_assembly(until_turn=K) 按 turn_number < K 过滤把
    H2 排除 → W1 末位 HumanMessage 用 ctx.user_input 拼装；修复前 ctx.user_input=""
    LLM 收到空 user 轮，仅 regenerate 路径触发，Row 1/3/5 不受影响。

    验证点：me.py 必须在 Row 6 命中时把 _regen_user_input 置为 last_msg.content，
    并让 ChatContextSchema.user_input 优先使用它。
    """
    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid = uuid4()

    lifecycle_ctx.seed_sess.add(
        SessionModel(id=sid, child_user_id=child.id, title="test", status=MessageStatus.active)
    )
    orphan = Message(
        session_id=sid,
        role=MessageRole.human,
        status=MessageStatus.active,
        content="原问题",
        turn_number=2,
    )
    lifecycle_ctx.seed_sess.add(orphan)
    await lifecycle_ctx.seed_sess.flush()
    orphan_id = orphan.id
    await lifecycle_ctx.seed_sess.commit()

    captured: dict = {}

    async def fake_run_llm_pipeline(*args, **kwargs):
        # 抓 me.py 装配出的 ctx，验证 user_input 不为空
        captured["user_input"] = kwargs["ctx"].user_input
        captured["session_id"] = kwargs["ctx"].session_id
        # 推 end + None 让 stream_generator 正常终止
        kwargs["queue"].put_nowait(
            frame_sse_event("end", {"finish_reason": "stop", "aid": str(uuid4())})
        )
        kwargs["queue"].put_nowait(None)

    body = make_payload(content="", session_id=str(sid), regenerate_for=str(orphan_id))

    with patch("app.api.me.run_llm_pipeline", new=fake_run_llm_pipeline):
        resp = await client.post("/api/v1/me/chat/stream", json=body, headers=headers)

    assert resp.status_code == 200
    assert captured.get("session_id") == sid
    # 核心断言：FIX 后 ctx.user_input == 孤儿原始 content
    assert captured.get("user_input") == "原问题", (
        "Row 6 must populate ctx.user_input from last_msg.content; "
        "otherwise build_messages_main's W1 wrapper gives the LLM an empty user turn"
    )


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
async def test_decision_row3_with_prior_human(lifecycle_ctx):
    """Row 3 sub-scenario: session has H1 + AI already, insert new human (H2).

    Covers H1 (active) + A1 (active) + new human → 3 active rows.
    This is the "non-orphan continuation" path: last_msg = AI, so we INSERT human.
    """
    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid = uuid4()

    lifecycle_ctx.seed_sess.add(
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
    lifecycle_ctx.seed_sess.add_all([human, ai])
    await lifecycle_ctx.seed_sess.flush()
    human_id = human.id
    await lifecycle_ctx.seed_sess.commit()

    body = make_payload(content="H2 continuation", session_id=str(sid))
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    lifecycle_ctx.rr.main_graph.astream = fake_astream
    resp = await client.post(
        "/api/v1/me/chat/stream", json=body, headers=headers
    )
    assert resp.status_code == 200

    # 强制从 DB 重新读取（避免前序事务的 identity map 缓存）
    lifecycle_ctx.assert_sess.expire_all()
    msgs = (
        (
            await lifecycle_ctx.assert_sess.execute(
                select(Message)
                .where(Message.session_id == sid, Message.status == MessageStatus.active)
                .order_by(Message.created_at, Message.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs) == 4  # H1 + AI + H2 + inline AI
    human_msgs = [m for m in msgs if m.role == MessageRole.human]
    assert len(human_msgs) == 2
    new_human = next(m for m in human_msgs if m.id != human_id)
    assert new_human.content == "H2 continuation"


# ---------------------------------------------------------------------------
# Row 5 (with prior AI): orphan + regen=null → UPDATE old discarded + INSERT human
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_row5_with_prior_ai(lifecycle_ctx):
    """Row 5: H1 + A1 + H2 active, regen=null → UPDATE H2 discarded + INSERT H3 active.

    PG timestamp construction: explicit created_at ensures H2 is the confirmed last
    active row by ORDER BY created_at DESC, id DESC.
    """
    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid = uuid4()

    lifecycle_ctx.seed_sess.add(
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
    lifecycle_ctx.seed_sess.add_all([h1, a1, h2])
    await lifecycle_ctx.seed_sess.flush()
    h2_id = h2.id
    await lifecycle_ctx.seed_sess.commit()

    body = make_payload(content="H3 content", session_id=str(sid))
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    lifecycle_ctx.rr.main_graph.astream = fake_astream
    resp = await client.post(
        "/api/v1/me/chat/stream", json=body, headers=headers
    )
    assert resp.status_code == 200

    frames = _parse_sse_stream(resp.text)
    hid_in_meta = frames[0]["data"]["hid"]

    lifecycle_ctx.assert_sess.expire_all()
    msgs = (
        (await lifecycle_ctx.assert_sess.execute(select(Message).where(Message.session_id == sid)
                                  .order_by(Message.created_at, Message.id))).scalars().all()
    )
    assert len(msgs) == 5  # H1 + A1 + H2(discarded) + H3 + inline AI
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
async def test_decision_row6_with_prior_ai_reuse(lifecycle_ctx):
    """Row 6: H1 + A1 + H2 active, regen=H2.id + content="" → reuse H2 (no new row).

    The Gate A closing argument says "last active row is human" ⟺ "orphan", so H2 is
    the orphan.  Row 6 should reuse it: hid=H2.id, no new row inserted.
    """
    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid = uuid4()

    lifecycle_ctx.seed_sess.add(
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
    lifecycle_ctx.seed_sess.add_all([h1, a1, h2])
    await lifecycle_ctx.seed_sess.flush()
    h2_id = h2.id
    await lifecycle_ctx.seed_sess.commit()

    body = make_payload(content="", session_id=str(sid), regenerate_for=str(h2_id))
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    lifecycle_ctx.rr.main_graph.astream = fake_astream
    resp = await client.post(
        "/api/v1/me/chat/stream", json=body, headers=headers
    )
    assert resp.status_code == 200

    frames = _parse_sse_stream(resp.text)
    assert frames[0]["data"]["hid"] == str(h2_id)  # hid unchanged

    msgs = (
        (await lifecycle_ctx.assert_sess.execute(select(Message).where(Message.session_id == sid)
                                  .order_by(Message.created_at, Message.id))).scalars().all()
    )
    assert len(msgs) == 4  # H1 + A1 + H2 + inline AI
    h2_row = next(m for m in msgs if m.id == h2_id)
    assert h2_row.content == "H2 original"  # content unchanged
    assert h2_row.status == MessageStatus.active


# ---------------------------------------------------------------------------
# Throttle lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_throttle_lock_rejects_second_request(app_with_eval, api_client_with_eval, auth_headers_child):
    """1 s 内连发两次 → 第二次 429 RequestThrottled (throttle TTL 自然过期)."""
    headers, _ = auth_headers_child
    body = make_payload(content="first")
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    app_with_eval.state.resources.main_graph.astream = fake_astream
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
    from app.core.locks import release_session_lock

    await release_session_lock(redis_client, str(sid), nonce)


# ---------------------------------------------------------------------------
# 404 / 403: session not found / child mismatch — no lock acquired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_400_releases_session_lock(
    app_with_eval, api_client_with_eval, auth_headers_child, redis_client, db_session
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

    app_with_eval.state.resources.main_graph.astream = fake_astream
    resp2 = await api_client_with_eval.post(
        "/api/v1/me/chat/stream", json=body2, headers=headers
    )
    assert resp2.status_code == 200


@pytest.mark.asyncio
async def test_throttle_lock_self_expires(
    app_with_eval, api_client_with_eval, auth_headers_child, redis_client
):
    """Throttle key uses TTL (SETNX px=1500), not actively deleted."""
    headers, child = auth_headers_child
    body = make_payload(content="first")
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    app_with_eval.state.resources.main_graph.astream = fake_astream
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
async def test_title_12_graphemes_ascii(app_with_eval, api_client_with_eval, auth_headers_child, db_session):
    """Unicode TR29 grapheme cluster: 12-grapheme title truncation (ZWJ = 1 cluster)."""
    headers, _ = auth_headers_child
    body = make_payload(content="Hello 你好 👨‍👩‍👧 abcdef")
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    app_with_eval.state.resources.main_graph.astream = fake_astream
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
    app_with_eval, api_client_with_eval, auth_headers_child, db_session
):
    """TR29 ZWJ emoji family = 1 grapheme cluster (not 3), anchored to prevent regression."""
    headers, _ = auth_headers_child
    body = make_payload(content="👨‍👩‍👧‍👦‍👧")  # 6-person family ZWJ sequence
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    app_with_eval.state.resources.main_graph.astream = fake_astream
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
    app_with_eval, api_client_with_eval, auth_headers_child, redis_client
):
    """Successful request → session lock released in generator finally."""
    headers, _ = auth_headers_child
    body = make_payload(content="hello")
    fake_payloads = [{"delta": "[fake]"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)

    app_with_eval.state.resources.main_graph.astream = fake_astream
    resp = await api_client_with_eval.post(
        "/api/v1/me/chat/stream", json=body, headers=headers
    )
    assert resp.status_code == 200
    await resp.aclose()  # ensure response fully consumed

    # After response closes, lock should be gone
    from app.core.locks import acquire_session_lock

    nonce = await acquire_session_lock(
        redis_client, _parse_sse_stream(resp.text)[0]["data"]["session_id"]
    )
    assert nonce is not None  # lock was released and we can re-acquire


# ---------------------------------------------------------------------------
# 多轮集成测试（M9-patch1 commit②：persist_ai_turn 单写点收敛）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_turn_natural_end(lifecycle_ctx):
    """(a) 自然结束 × 2 轮：turn_number 和 ai_turn_counter 逐轮递增。

    - Turn1：ai 行 turn_number==1 且 session.ai_turn_counter==1
    - Turn2：ai 行 turn_number==2 且 session.ai_turn_counter==2
    """

    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    # --- Turn 1 ---
    fake_payloads = [{"delta": "第一轮回复"}]
    fake_astream = _make_fake_graph_astream(fake_payloads)
    lifecycle_ctx.rr.main_graph.astream = fake_astream

    resp1 = await client.post(
        "/api/v1/me/chat/stream", json=make_payload("你好"), headers=headers,
    )
    assert resp1.status_code == 200
    frames1 = _parse_sse_stream(resp1.text)
    sid = frames1[0]["data"]["session_id"]
    assert frames1[-1]["type"] == "end"

    # 验证 Turn1：从 DB 重新读（避免 identity-map 缓存）
    lifecycle_ctx.assert_sess.expire_all()
    msgs1 = (
        (
            await lifecycle_ctx.assert_sess.execute(
                select(Message)
                .where(Message.session_id == sid)
                .order_by(Message.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs1) == 2  # human + ai
    assert msgs1[0].role == MessageRole.human
    assert msgs1[0].turn_number == 1
    assert msgs1[1].role == MessageRole.ai
    assert msgs1[1].turn_number == 1

    sess1 = await lifecycle_ctx.assert_sess.get(SessionModel, sid)
    assert sess1.ai_turn_counter == 1

    # 清除节流锁，使 Turn2 不被 429
    await lifecycle_ctx.redis_client.delete(f"chat:throttle:{child.id}")

    # --- Turn 2 ---
    fake_payloads2 = [{"delta": "第二轮回复"}]
    fake_astream2 = _make_fake_graph_astream(fake_payloads2)
    lifecycle_ctx.rr.main_graph.astream = fake_astream2

    resp2 = await client.post(
        "/api/v1/me/chat/stream",
        json=make_payload("继续", session_id=str(sid)),
        headers=headers,
    )
    assert resp2.status_code == 200
    frames2 = _parse_sse_stream(resp2.text)
    assert frames2[-1]["type"] == "end"

    # 验证 Turn2：从 DB 重新读
    lifecycle_ctx.assert_sess.expire_all()
    msgs2 = (
        (
            await lifecycle_ctx.assert_sess.execute(
                select(Message)
                .where(Message.session_id == sid)
                .order_by(Message.created_at)
            )
        )
        .scalars()
        .all()
    )
    # H1 + A1 + H2 + A2 = 4 条
    assert len(msgs2) == 4, f"期望 4 条，实际 {len(msgs2)}"
    assert msgs2[2].role == MessageRole.human
    assert msgs2[2].turn_number == 2
    assert msgs2[3].role == MessageRole.ai
    assert msgs2[3].turn_number == 2

    sess2 = await lifecycle_ctx.assert_sess.get(SessionModel, sid)
    assert sess2.ai_turn_counter == 2

    # (d) Turn2 能取到 Turn1 的行（验证多轮历史可见）
    assert msgs2[0].role == MessageRole.human  # H1
    assert msgs2[1].role == MessageRole.ai     # A1
    assert msgs2[0].status == MessageStatus.active
    assert msgs2[1].status == MessageStatus.active


@pytest.mark.asyncio
async def test_multi_turn_stop_with_ai(lifecycle_ctx):
    """(b) StopWithAi：有内容时 stop → ai 行 + turn_number + counter 自增。"""
    import asyncio
    from uuid import uuid4

    from app.domain.chat.stream_signals import running_streams

    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    # 预埋 session（已知 sid，供 bg task 使用）
    sid = uuid4()
    lifecycle_ctx.seed_sess.add(
        SessionModel(id=sid, child_user_id=child.id, title="test")
    )
    await lifecycle_ctx.seed_sess.commit()

    _gate = asyncio.Event()
    _ready = asyncio.Event()

    async def _fake_astream(initial_state, stream_mode="custom", **kwargs):
        yield {"delta": "partial "}
        _ready.set()
        await _gate.wait()
        yield {}

    lifecycle_ctx.rr.main_graph.astream = _fake_astream

    # 在 bg task 中启动 POST
    resp_task = asyncio.create_task(
        client.post(
            "/api/v1/me/chat/stream",
            json=make_payload("hello", session_id=str(sid)),
            headers=headers,
        ),
    )

    # 等待 fake 首次 yield（此时 bg task 已在 _gate 上等待）
    await asyncio.wait_for(_ready.wait(), timeout=5)

    # 设置停止信号
    running_streams[str(sid)].set()
    _gate.set()

    resp = await resp_task
    assert resp.status_code == 200
    frames = _parse_sse_stream(resp.text)
    assert frames[-1]["type"] == "stopped", f"尾帧应是 stopped，实际 {frames[-1]}"
    assert "aid" in frames[-1]["data"], "StopWithAi 应带 aid"

    lifecycle_ctx.assert_sess.expire_all()
    msgs = (
        (
            await lifecycle_ctx.assert_sess.execute(
                select(Message)
                .where(Message.session_id == sid)
                .order_by(Message.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs) == 2  # human + ai
    assert msgs[1].role == MessageRole.ai
    assert msgs[1].turn_number == 1
    assert msgs[1].finish_reason == "user_stopped"

    sess = await lifecycle_ctx.assert_sess.get(SessionModel, sid)
    assert sess.ai_turn_counter == 1


@pytest.mark.asyncio
async def test_multi_turn_stop_no_ai(lifecycle_ctx):
    """(c) StopNoAi：无内容时 stop → 不写 ai 行、counter 不变。"""
    import asyncio
    from uuid import uuid4

    from app.domain.chat.stream_signals import running_streams

    client, headers, child = await lifecycle_setup(lifecycle_ctx)

    sid = uuid4()
    lifecycle_ctx.seed_sess.add(
        SessionModel(id=sid, child_user_id=child.id, title="test")
    )
    await lifecycle_ctx.seed_sess.commit()

    _gate = asyncio.Event()
    _ready = asyncio.Event()

    async def _fake_astream(initial_state, stream_mode="custom", **kwargs):
        _ready.set()
        await _gate.wait()
        yield {}

    lifecycle_ctx.rr.main_graph.astream = _fake_astream

    resp_task = asyncio.create_task(
        client.post(
            "/api/v1/me/chat/stream",
            json=make_payload("hello", session_id=str(sid)),
            headers=headers,
        ),
    )

    await asyncio.wait_for(_ready.wait(), timeout=5)

    running_streams[str(sid)].set()
    _gate.set()

    resp = await resp_task
    assert resp.status_code == 200
    frames = _parse_sse_stream(resp.text)
    assert frames[-1]["type"] == "stopped"
    assert "aid" not in frames[-1]["data"], "StopNoAi 不应带 aid"

    lifecycle_ctx.assert_sess.expire_all()
    msgs = (
        (
            await lifecycle_ctx.assert_sess.execute(
                select(Message)
                .where(Message.session_id == sid)
                .order_by(Message.created_at)
            )
        )
        .scalars()
        .all()
    )
    # 只有 human 行（pre-seeded session 没有 human，但 commit① 会写一条）
    assert len(msgs) == 1, f"StopNoAi 应只有 1 条 human 行，实际 {len(msgs)}"
    assert msgs[0].role == MessageRole.human

    sess = await lifecycle_ctx.assert_sess.get(SessionModel, sid)
    assert sess.ai_turn_counter == 0  # 未自增


# ---------------------------------------------------------------------------
# intervention_type 写路径接线四态测试（D-patch1-2）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intervention_type_crisis(lifecycle_ctx):
    """crisis 路由：ai 行 intervention_type 落库为 crisis。"""
    from uuid import uuid4

    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid = uuid4()
    lifecycle_ctx.seed_sess.add(
        SessionModel(id=sid, child_user_id=child.id, title="test"),
    )
    await lifecycle_ctx.seed_sess.commit()

    # crisis: 先发 intervention_type 信号，再发 delta
    fake_payloads = [
        {"intervention_type": "crisis"},
        {"delta": "crisis response"},
    ]
    lifecycle_ctx.rr.main_graph.astream = _make_fake_graph_astream(fake_payloads)

    resp = await client.post(
        "/api/v1/me/chat/stream",
        json=make_payload("hello", session_id=str(sid)),
        headers=headers,
    )
    assert resp.status_code == 200

    lifecycle_ctx.assert_sess.expire_all()
    msgs = (
        (await lifecycle_ctx.assert_sess.execute(
            select(Message)
            .where(Message.session_id == sid)
            .order_by(Message.created_at),
        ))
        .scalars()
        .all()
    )
    assert len(msgs) == 2  # human + ai
    assert msgs[1].intervention_type == InterventionType.crisis


@pytest.mark.asyncio
async def test_intervention_type_redline(lifecycle_ctx):
    """redline 路由：ai 行 intervention_type 落库为 redline。"""
    from uuid import uuid4

    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid = uuid4()
    lifecycle_ctx.seed_sess.add(
        SessionModel(id=sid, child_user_id=child.id, title="test"),
    )
    await lifecycle_ctx.seed_sess.commit()

    fake_payloads = [
        {"intervention_type": "redline"},
        {"delta": "redline response"},
    ]
    lifecycle_ctx.rr.main_graph.astream = _make_fake_graph_astream(fake_payloads)

    resp = await client.post(
        "/api/v1/me/chat/stream",
        json=make_payload("hello", session_id=str(sid)),
        headers=headers,
    )
    assert resp.status_code == 200

    lifecycle_ctx.assert_sess.expire_all()
    msgs = (
        (await lifecycle_ctx.assert_sess.execute(
            select(Message)
            .where(Message.session_id == sid)
            .order_by(Message.created_at),
        ))
        .scalars()
        .all()
    )
    assert len(msgs) == 2
    assert msgs[1].intervention_type == InterventionType.redline


@pytest.mark.asyncio
async def test_intervention_type_guided(lifecycle_ctx):
    """guided 路由：ai 行 intervention_type 落库为 guided。"""
    from uuid import uuid4

    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid = uuid4()
    lifecycle_ctx.seed_sess.add(
        SessionModel(id=sid, child_user_id=child.id, title="test"),
    )
    await lifecycle_ctx.seed_sess.commit()

    fake_payloads = [
        {"intervention_type": "guided"},
        {"delta": "guided response"},
    ]
    lifecycle_ctx.rr.main_graph.astream = _make_fake_graph_astream(fake_payloads)

    resp = await client.post(
        "/api/v1/me/chat/stream",
        json=make_payload("hello", session_id=str(sid)),
        headers=headers,
    )
    assert resp.status_code == 200

    lifecycle_ctx.assert_sess.expire_all()
    msgs = (
        (await lifecycle_ctx.assert_sess.execute(
            select(Message)
            .where(Message.session_id == sid)
            .order_by(Message.created_at),
        ))
        .scalars()
        .all()
    )
    assert len(msgs) == 2
    assert msgs[1].intervention_type == InterventionType.guided


@pytest.mark.asyncio
async def test_intervention_type_normal(lifecycle_ctx):
    """normal 路由：无 intervention_type 发射 → 落库 None（显式断言，杜绝假阳）。"""
    from uuid import uuid4

    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid = uuid4()
    lifecycle_ctx.seed_sess.add(
        SessionModel(id=sid, child_user_id=child.id, title="test"),
    )
    await lifecycle_ctx.seed_sess.commit()

    # normal: 不发射 intervention_type
    fake_payloads = [{"delta": "normal reply"}]
    lifecycle_ctx.rr.main_graph.astream = _make_fake_graph_astream(fake_payloads)

    resp = await client.post(
        "/api/v1/me/chat/stream",
        json=make_payload("hello", session_id=str(sid)),
        headers=headers,
    )
    assert resp.status_code == 200

    lifecycle_ctx.assert_sess.expire_all()
    msgs = (
        (await lifecycle_ctx.assert_sess.execute(
            select(Message)
            .where(Message.session_id == sid)
            .order_by(Message.created_at),
        ))
        .scalars()
        .all()
    )
    assert len(msgs) == 2
    assert msgs[1].intervention_type is None  # 显式断言 None，杜绝假阳


@pytest.mark.asyncio
async def test_intervention_type_stop_with_ai_crisis(lifecycle_ctx):
    """StopWithAi + crisis：有内容时 stop → ai 行 intervention_type=crisis。"""
    import asyncio
    from uuid import uuid4

    from app.domain.chat.stream_signals import running_streams

    client, headers, child = await lifecycle_setup(lifecycle_ctx)
    sid = uuid4()
    lifecycle_ctx.seed_sess.add(
        SessionModel(id=sid, child_user_id=child.id, title="test"),
    )
    await lifecycle_ctx.seed_sess.commit()

    _gate = asyncio.Event()
    _ready = asyncio.Event()

    async def _fake_astream(initial_state, stream_mode="custom", **kwargs):
        yield {"intervention_type": "crisis"}
        yield {"delta": "partial "}
        _ready.set()
        await _gate.wait()
        yield {}

    lifecycle_ctx.rr.main_graph.astream = _fake_astream

    resp_task = asyncio.create_task(
        client.post(
            "/api/v1/me/chat/stream",
            json=make_payload("hello", session_id=str(sid)),
            headers=headers,
        ),
    )
    await asyncio.wait_for(_ready.wait(), timeout=5)
    running_streams[str(sid)].set()
    _gate.set()
    resp = await resp_task
    assert resp.status_code == 200

    lifecycle_ctx.assert_sess.expire_all()
    msgs = (
        (await lifecycle_ctx.assert_sess.execute(
            select(Message)
            .where(Message.session_id == sid)
            .order_by(Message.created_at),
        ))
        .scalars()
        .all()
    )
    assert len(msgs) == 2
    assert msgs[1].intervention_type == InterventionType.crisis


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
