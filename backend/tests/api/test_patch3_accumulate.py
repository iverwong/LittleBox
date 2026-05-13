"""Groups 5+6+8：usage 快照 + LLM 消息拦截 + needs_compression 标志 + 阻塞压缩。

所有测试通过 monkeypatch 绕过 throttle + session lock，不依赖 eval patch。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
pytestmark = pytest.mark.asyncio(loop_scope="function")  # 覆盖 pyproject.toml 的 session 级 loop scope
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.auth.redis_ops import commit_with_redis
from app.auth.tokens import issue_token
from app.chat.graph import main_graph
from app.db import get_db
from app.models.accounts import ChildProfile, Family, FamilyMember, User
from app.models.chat import Message
from app.models.chat import Session as SessionModel
from app.models.enums import Gender, MessageRole, MessageStatus, UserRole

SHANGHAI = ZoneInfo("Asia/Shanghai")


# ---- fixtures（不依赖 eval patch，改用 monkeypatch）----


@pytest.fixture
async def app(db_session, redis_client):
    from app.auth.redis_client import get_redis
    from app.main import create_app

    application = create_app()

    async def _get_db():
        yield db_session

    async def _get_redis():
        return redis_client

    application.dependency_overrides[get_db] = _get_db
    application.dependency_overrides[get_redis] = _get_redis
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
async def api_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
async def child_with_profile(db_session):
    """Child + family + ChildProfile（含 birth_date / gender）。"""
    fam = Family()
    db_session.add(fam)
    await db_session.flush()
    user = User(family_id=fam.id, role=UserRole.child, phone="0005", is_active=True)
    db_session.add(user)
    await db_session.flush()
    db_session.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.child))
    profile = ChildProfile(
        child_user_id=user.id,
        created_by=user.id,
        birth_date=datetime(2016, 6, 1, tzinfo=SHANGHAI).date(),
        gender=Gender.male,
        nickname="test",
    )
    db_session.add(profile)
    await db_session.commit()
    return user


@pytest.fixture
async def auth_headers_child(db_session, redis_client, child_with_profile):
    device_id = "test-device-acc"
    token = await issue_token(
        db_session, user_id=child_with_profile.id, role=UserRole.child,
        family_id=child_with_profile.family_id, device_id=device_id, ttl_days=None,
    )
    await commit_with_redis(db_session, redis_client)
    headers = {"Authorization": f"Bearer {token}", "X-Device-Id": device_id}
    return headers, child_with_profile


@pytest.fixture(autouse=True)
def _patch_locks(monkeypatch: pytest.MonkeyPatch):
    """绕过 throttle + session lock（避免依赖 FakeRedis eval patch 跨测试泄漏）。"""
    from app.chat.locks import acquire_session_lock, acquire_throttle_lock
    monkeypatch.setattr("app.api.me.acquire_throttle_lock", AsyncMock(return_value=True))
    monkeypatch.setattr("app.api.me.acquire_session_lock", AsyncMock(return_value="mock-nonce"))
    monkeypatch.setattr("app.api.me.release_session_lock", AsyncMock(return_value=None))


def make_payload(content: str, session_id: str | None = None, regenerate_for: str | None = None):
    payload = {"content": content}
    if session_id is not None:
        payload["session_id"] = session_id
    if regenerate_for is not None:
        payload["regenerate_for"] = regenerate_for
    return payload


def _parse_sse_frames(raw: str) -> list[dict]:
    import json as _json
    events = []
    current_type = None
    for line in raw.split("\n"):
        if line.startswith("event:"):
            current_type = line[len("event:"):].strip()
        elif line.startswith("data:") and current_type is not None:
            events.append({"type": current_type, "data": _json.loads(line[len("data:"):].strip())})
    return events


# ---- Group 6: orphan discard + LLM messages 拦截（去掉 token 累加断言）----

@pytest.mark.asyncio
async def test_discarded_orphan_llm_messages(api_client, auth_headers_child, db_session):
    """孤儿 human 改内容重发：LLM 收到不含旧 orphan 的 messages。（Group 6）"""
    headers, child = auth_headers_child
    sentinel_messages: list = []

    sid = uuid.uuid4()
    session = SessionModel(
        id=sid, child_user_id=child.id, title="孤儿测试",
        status="active",
    )
    db_session.add(session)
    orphan_msg = Message(
        session_id=sid, role=MessageRole.human, content="我想问 A",
        status=MessageStatus.active,
    )
    db_session.add(orphan_msg)
    await db_session.flush()
    orphan_id = orphan_msg.id
    await db_session.commit()

    async def fake_astream_capture(initial_state, stream_mode="custom"):
        nonlocal sentinel_messages
        sentinel_messages = list(initial_state["messages"])
        for p in [{"delta": "[AI回复B]"}, {"finish_reason": "stop"}]:
            yield p

    with patch.object(main_graph, "astream", fake_astream_capture):
        body = make_payload(content="我想问 B", session_id=str(sid))
        resp = await api_client.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200
        await resp.aclose()

    msgs_after = (
        (await db_session.execute(
            select(Message).where(Message.session_id == sid).order_by(Message.created_at)
        ))
        .scalars()
        .all()
    )
    orphan_after = next(m for m in msgs_after if m.id == orphan_id)
    assert orphan_after.status == MessageStatus.discarded

    # ---- LLM messages 拦截验证 ----
    msgs_captured = sentinel_messages
    assert msgs_captured[0].type == "system"
    for m in msgs_captured:
        content = getattr(m, "content", "")
        assert "我想问 A" not in content, f"orphan content leaked: {content}"
    new_contents = [getattr(m, "content", "") for m in msgs_captured]
    assert any("我想问 B" in c for c in new_contents), "new human content missing"


# ---- Group 8: 阈值 → needs_compression 标志 ----

@pytest.mark.asyncio
async def test_threshold_sets_needs_compression_flag(api_client, auth_headers_child, db_session):
    """usage_metadata ≥ 500_000 → session.needs_compression = True。（Group 8）"""
    headers, child = auth_headers_child
    sid = uuid.uuid4()
    session = SessionModel(
        id=sid, child_user_id=child.id, title="阈值测试",
        status="active",
    )
    db_session.add(session)
    await db_session.commit()

    async def fake_astream(initial_state, stream_mode="custom"):
        for p in [
            {"delta": "x"},
            {"finish_reason": "stop"},
            {"usage_metadata": {"input_tokens": 300_000, "output_tokens": 200_001, "total_tokens": 500_001}},
        ]:
            yield p

    with patch.object(main_graph, "astream", fake_astream):
        body = make_payload(content="触发阈值", session_id=str(sid))
        resp = await api_client.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200
        await resp.aclose()

    session_after = await db_session.get(SessionModel, sid)
    assert session_after.context_size_tokens is not None, "context_size_tokens should be set"
    total_usage = 300_000 + 200_001
    assert session_after.context_size_tokens == total_usage, (
        f"expected {total_usage}, got {session_after.context_size_tokens}"
    )
    assert session_after.needs_compression is True, "flag should be True when threshold exceeded"


# ---- Group 9: 压缩 ----

@pytest.mark.asyncio
async def test_context_size_tokens_snapshot_not_accumulate(api_client, auth_headers_child, db_session):
    """两轮对话：context_size_tokens 是末轮 usage 快照，不是累积。（Group 9）"""
    headers, child = auth_headers_child
    sess_uuid = uuid.uuid4()
    db_session.add(SessionModel(
        id=sess_uuid, child_user_id=child.id, title="快照测试",
        status="active",
    ))
    await db_session.commit()

    async def fake_stream_round1(initial_state, stream_mode="custom"):
        for p in [
            {"delta": "第一轮"},
            {"finish_reason": "stop"},
            {"usage_metadata": {"input_tokens": 100_000, "output_tokens": 50_000, "total_tokens": 150_000}},
        ]:
            yield p

    with patch.object(main_graph, "astream", fake_stream_round1):
        body = make_payload(content="第一轮", session_id=str(sess_uuid))
        resp = await api_client.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200
        await resp.aclose()

    s1 = await db_session.get(SessionModel, sess_uuid)
    assert s1.context_size_tokens == 150_000

    async def fake_stream_round2(initial_state, stream_mode="custom"):
        for p in [
            {"delta": "第二轮"},
            {"finish_reason": "stop"},
            {"usage_metadata": {"input_tokens": 400_000, "output_tokens": 100_000, "total_tokens": 500_000}},
        ]:
            yield p

    with patch.object(main_graph, "astream", fake_stream_round2):
        body = make_payload(content="第二轮", session_id=str(sess_uuid))
        resp = await api_client.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200
        await resp.aclose()

    s2 = await db_session.get(SessionModel, sess_uuid)
    assert s2.context_size_tokens == 500_000, "should be round2's snapshot, NOT cumulative 650k"


@pytest.mark.asyncio
async def test_compression_progress_fired_when_flag_true(api_client, auth_headers_child, db_session):
    """needs_compression=True 时 user 到达后先发 compression_progress 帧。（Group 9）"""
    headers, child = auth_headers_child
    sid = uuid.uuid4()
    db_session.add(SessionModel(
        id=sid, child_user_id=child.id, title="压缩测试",
        status="active", needs_compression=True,
    ))
    await db_session.commit()

    # mock get_chat_llm 给压缩用；fake astream 给主图用
    mock_llm = AsyncMock()
    mock_llm.ainvoke.return_value.content = "压缩摘要"

    async def fake_astream(initial_state, stream_mode="custom"):
        for p in [{"delta": "回复"}, {"finish_reason": "stop"}]:
            yield p

    with (
        patch("app.chat.factory.get_chat_llm", return_value=mock_llm),
        patch.object(main_graph, "astream", fake_astream),
    ):
        body = make_payload(content="新消息", session_id=str(sid))
        resp = await api_client.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200
        await resp.aclose()

    frames = _parse_sse_frames(resp.text)
    assert any(f["type"] == "compression_progress" for f in frames), (
        "compression_progress should be emitted"
    )
    # 验证 active → compressed + new summary
    msgs = (await db_session.execute(
        select(Message).where(Message.session_id == sid).order_by(Message.created_at)
    )).scalars().all()
    assert any(m.role == MessageRole.summary for m in msgs), "summary message should exist"
    session = await db_session.get(SessionModel, sid)
    assert session.needs_compression is False, "flag reset after compression"


@pytest.mark.asyncio
async def test_compression_failure_keeps_flag(api_client, auth_headers_child, db_session):
    """压缩失败 → SSE error 帧 + needs_compression 保持 True。（Group 9）"""
    headers, child = auth_headers_child
    sid = uuid.uuid4()
    db_session.add(SessionModel(
        id=sid, child_user_id=child.id, title="压缩失败测试",
        status="active", needs_compression=True,
    ))
    await db_session.commit()

    # mock 压缩 LLM 抛错
    mock_llm = AsyncMock()
    mock_llm.ainvoke.side_effect = RuntimeError("模拟压缩失败")

    async def fake_astream(initial_state, stream_mode="custom"):
        for p in [{"delta": "回复"}, {"finish_reason": "stop"}]:
            yield p

    with (
        patch("app.chat.factory.get_chat_llm", return_value=mock_llm),
        patch.object(main_graph, "astream", fake_astream),
    ):
        body = make_payload(content="新消息", session_id=str(sid))
        resp = await api_client.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200
        await resp.aclose()

    frames = _parse_sse_frames(resp.text)
    assert any(f["type"] == "error" for f in frames), "error frame should be emitted on compression failure"
    session = await db_session.get(SessionModel, sid)
    assert session.needs_compression is True, "flag should stay True after failure"


@pytest.mark.asyncio
async def test_compression_skipped_when_flag_false(api_client, auth_headers_child, db_session):
    """纯新 session（needs_compression=False）不发 compression_progress。（Group 9）"""
    headers, child = auth_headers_child

    async def fake_astream(initial_state, stream_mode="custom"):
        for p in [{"delta": "你好"}, {"finish_reason": "stop"}]:
            yield p

    with patch.object(main_graph, "astream", fake_astream):
        body = make_payload(content="你好")
        resp = await api_client.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200
        await resp.aclose()

    assert b"compression_progress" not in resp.content, (
        "should NOT emit compression_progress when flag is False"
    )
