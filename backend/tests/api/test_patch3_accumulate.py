"""Groups 5+6+8：累加器两轮积累 + orphan discarded 回滚 + 阈值 log.warning。

所有测试通过 monkeypatch 绕过 throttle + session lock，不依赖 eval patch。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
import uuid
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
pytestmark = pytest.mark.asyncio(loop_scope="function")  # 覆盖 pyproject.toml 的 session 级 loop scope
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.auth.redis_ops import commit_with_redis
from app.auth.tokens import issue_token
from app.chat.compression import estimate_tokens
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


# ---- Group 5: 两轮积累 ----

@pytest.mark.asyncio
async def test_context_token_count_accumulates(api_client, auth_headers_child, db_session):
    """两轮对话后 context_token_count 正确累加。（Group 5）"""
    headers, child = auth_headers_child

    async def fake_astream(initial_state, stream_mode="custom"):
        for p in [{"delta": "[第一轮AI回复]"}, {"finish_reason": "stop"}]:
            yield p

    with patch.object(main_graph, "astream", fake_astream):
        body = make_payload(content="第一轮用户消息你好呀")
        resp = await api_client.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200
        await resp.aclose()
        frames = _parse_sse_frames(resp.text)
        sid = uuid.UUID(frames[0]["data"]["session_id"])

    session = await db_session.get(SessionModel, sid)
    assert session is not None
    expected_round1 = estimate_tokens("第一轮用户消息你好呀") + estimate_tokens("[第一轮AI回复]")
    assert session.context_token_count == expected_round1, (
        f"round1: expected {expected_round1}, got {session.context_token_count}"
    )

    # 第二轮
    async def fake_astream2(initial_state, stream_mode="custom"):
        for p in [{"delta": "[第二轮AI回复也]"}, {"finish_reason": "stop"}]:
            yield p

    with patch.object(main_graph, "astream", fake_astream2):
        body = make_payload(content="第二轮用户", session_id=str(sid))
        resp = await api_client.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200
        await resp.aclose()
        frames2 = _parse_sse_frames(resp.text)
        r2_sid = frames2[0]["data"]["session_id"]

    # 验证 Round 2 复用了 Round 1 的 session
    assert r2_sid == str(sid), f"Round 2 created new session {r2_sid}, expected {sid}"

    # 查 DB 中该 child 的所有 session
    all_sessions = (await db_session.execute(
        select(SessionModel).where(SessionModel.child_user_id == child.id)
    )).scalars().all()
    assert len(all_sessions) == 1, f"Expected 1 session, got {len(all_sessions)}"

    session2 = all_sessions[0]
    expected_round2 = expected_round1 + estimate_tokens("第二轮用户") + estimate_tokens("[第二轮AI回复也]")
    assert session2.context_token_count == expected_round2, (
        f"round2: expected {expected_round2}, got {session2.context_token_count}"
    )


# ---- Group 6: orphan discard + LLM messages 拦截 ----

@pytest.mark.asyncio
async def test_discarded_rollback_token_and_llm_messages(api_client, auth_headers_child, db_session):
    """孤儿 human 改内容重发：累加器正确扣减；LLM 收到不含旧 orphan 的 messages。（Group 6）

    补强断言：
    - initial_state["messages"] 不含旧 discarded orphan content
    - initial_state["messages"] 含新 HumanMessage(req.content)
    - initial_state["messages"][0] 为 SystemMessage
    """
    headers, child = auth_headers_child
    sentinel_messages: list = []

    # 预种：session + orphan human（无 AI，使 last_msg=human 触发 Row 5）
    sid = uuid.uuid4()
    session = SessionModel(
        id=sid, child_user_id=child.id, title="孤儿测试",
        status="active", context_token_count=estimate_tokens("我想问 A"),
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

    # 改内容重发（Row 5 orphan discard + token -=）
    async def fake_astream_capture(initial_state, stream_mode="custom"):
        nonlocal sentinel_messages
        sentinel_messages = list(initial_state["messages"])
        for p in [{"delta": "[AI回复B]"}, {"finish_reason": "stop"}]:
            yield p

    with patch.object(main_graph, "astream", fake_astream_capture):
        body = make_payload(content="我想问 B", session_id=str(sid))  # regen=None → Row 5
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
    # 旧 orphan 已 discarded
    orphan_after = next(m for m in msgs_after if m.id == orphan_id)
    assert orphan_after.status == MessageStatus.discarded

    # 累加器已扣减旧 orphan + 增新 human + 增 ai
    session_after = await db_session.get(SessionModel, sid)
    expected_token = estimate_tokens("我想问 B") + estimate_tokens("[AI回复B]")
    assert session_after.context_token_count == expected_token, (
        f"expected {expected_token}, got {session_after.context_token_count}"
    )

    # ---- 补强断言：LLM messages 拦截验证 ----
    msgs_captured = sentinel_messages
    # (1) SystemMessage 在首
    assert msgs_captured[0].type == "system"
    # (2) 不含旧 discarded orphan content
    for m in msgs_captured:
        content = getattr(m, "content", "")
        assert "我想问 A" not in content, f"orphan content leaked: {content}"
    # (3) 含新 HumanMessage
    new_contents = [getattr(m, "content", "") for m in msgs_captured]
    assert any("我想问 B" in c for c in new_contents), "new human content missing"


# ---- Group 8: 阈值 log.warning ----

@pytest.mark.asyncio
async def test_threshold_inline_log_warning(api_client, auth_headers_child, db_session, caplog):
    """context_token_count ≥ 500_000 → log.warning 触发。（Group 8）"""
    headers, child = auth_headers_child
    sid = uuid.uuid4()
    near_threshold = 499_999
    session = SessionModel(
        id=sid, child_user_id=child.id, title="阈值测试",
        status="active", context_token_count=near_threshold,
    )
    db_session.add(session)
    await db_session.commit()

    async def fake_astream(initial_state, stream_mode="custom"):
        for p in [{"delta": "x"}, {"finish_reason": "stop"}]:
            yield p

    with patch.object(main_graph, "astream", fake_astream):
        with caplog.at_level(logging.WARNING, logger="app.api.me"):
            body = make_payload(content="触发阈值", session_id=str(sid))
            resp = await api_client.post("/api/v1/me/chat/stream", json=body, headers=headers)
            assert resp.status_code == 200
            await resp.aclose()

    threshold_warnings = [r for r in caplog.records if "context exceeded threshold" in r.message]
    assert len(threshold_warnings) >= 1, "threshold log.warning not triggered"
    expected_total = near_threshold + estimate_tokens("x") + estimate_tokens("触发阈值")
    assert threshold_warnings[0].token_count == expected_total

    session_after = await db_session.get(SessionModel, sid)
    assert session_after.context_token_count == expected_total
