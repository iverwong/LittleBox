"""Group 4：首次 chat_stream → 新建 session，标题匹配中文格式。"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest
pytestmark = pytest.mark.asyncio(loop_scope="function")  # 覆盖 pyproject.toml 的 session 级 loop scope
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.auth.redis_ops import commit_with_redis
from app.auth.tokens import issue_token
from app.chat.graph import main_graph
from app.db import get_db
from app.models.accounts import Family, FamilyMember, User
from app.models.chat import Session as SessionModel
from app.models.enums import UserRole


# ---- fixtures (with eval patch for session lock) ----


@pytest.fixture
def redis_client_with_eval(redis_client):
    import fakeredis.aioredis

    async def mock_eval(self, script, num_keys, key, nonce_arg):
        stored = await self.get(key)
        if stored == nonce_arg:
            await self.delete(key)
            return 1
        return 0

    original = fakeredis.aioredis.FakeRedis.eval
    fakeredis.aioredis.FakeRedis.eval = mock_eval
    yield redis_client
    fakeredis.aioredis.FakeRedis.eval = original


@pytest.fixture
async def app_with_eval(db_session, redis_client_with_eval):
    from app.auth.redis_client import get_redis
    from app.main import create_app

    application = create_app()

    async def _get_db():
        yield db_session

    async def _get_redis():
        return redis_client_with_eval

    application.dependency_overrides[get_db] = _get_db
    application.dependency_overrides[get_redis] = _get_redis
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
async def api_client(app_with_eval):
    transport = ASGITransport(app=app_with_eval)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
async def child_user(db_session):
    fam = Family()
    db_session.add(fam)
    await db_session.flush()
    user = User(family_id=fam.id, role=UserRole.child, phone="0001", is_active=True)
    db_session.add(user)
    await db_session.flush()
    db_session.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.child))
    await db_session.commit()
    return user


@pytest.fixture
async def auth_headers_child(db_session, redis_client, child_user):
    device_id = "test-device-g4"
    token = await issue_token(
        db_session, user_id=child_user.id, role=UserRole.child,
        family_id=child_user.family_id, device_id=device_id, ttl_days=None,
    )
    await commit_with_redis(db_session, redis_client)
    headers = {"Authorization": f"Bearer {token}", "X-Device-Id": device_id}
    return headers, child_user


def make_payload(content: str):
    return {"content": content}


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


# ---- Group 4 ----

@pytest.mark.asyncio
async def test_first_message_creates_session(api_client, auth_headers_child, db_session):
    """首次消息 → 新 session，标题 √± 格式。（Group 4）"""
    headers, child = auth_headers_child

    async def fake_astream(initial_state, stream_mode="custom"):
        for p in [{"delta": "[fake]"}, {"finish_reason": "stop"}]:
            yield p

    with patch.object(main_graph, "astream", fake_astream):
        body = make_payload(content="你好")
        resp = await api_client.post("/api/v1/me/chat/stream", json=body, headers=headers)
        assert resp.status_code == 200

        frames = _parse_sse_frames(resp.text)
        sid = frames[0]["data"]["session_id"]

    session = await db_session.get(SessionModel, sid)
    assert session is not None
    assert session.child_user_id == child.id
    assert session.status == "active"
    # 标题中文格式：周X · M月D日
    assert re.match(r"^周[一二三四五六日] · \d+月\d+日$", session.title), (
        f"unexpected title: {session.title}"
    )
