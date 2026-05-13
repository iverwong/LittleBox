"""Group 3：跨硬切点 04:00 时 last_active_at = user_msg.created_at 触发新建 session。

Row 1 分支前查 latest_session，should_switch_session 判跨日 → 新建。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
pytestmark = pytest.mark.asyncio(loop_scope="function")  # 覆盖 pyproject.toml 的 session 级 loop scope
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.auth.redis_ops import commit_with_redis
from app.auth.tokens import issue_token
from app.chat.graph import main_graph
from app.db import get_db
from app.models.accounts import Family, FamilyMember, User
from app.models.chat import Message
from app.models.chat import Session as SessionModel
from app.models.enums import MessageRole, MessageStatus, UserRole

SHANGHAI = ZoneInfo("Asia/Shanghai")


# ---- fixtures ----


@pytest.fixture
def redis_client_with_eval(redis_client: FakeRedis) -> FakeRedis:
    import fakeredis.aioredis

    async def mock_eval(self, script: str, num_keys: int, key: str, nonce_arg: str) -> int:
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
async def api_client_with_eval(app_with_eval):
    transport = ASGITransport(app=app_with_eval)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
async def child_user(db_session):
    fam = Family()
    db_session.add(fam)
    await db_session.flush()
    user = User(family_id=fam.id, role=UserRole.child, phone="0000", is_active=True)
    db_session.add(user)
    await db_session.flush()
    db_session.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.child))
    await db_session.commit()
    return user


@pytest.fixture
async def auth_headers_child(db_session, redis_client_with_eval, child_user):
    device_id = "test-device-g3"
    token = await issue_token(
        db_session, user_id=child_user.id, role=UserRole.child,
        family_id=child_user.family_id, device_id=device_id, ttl_days=None,
    )
    await commit_with_redis(db_session, redis_client_with_eval)
    headers = {"Authorization": f"Bearer {token}", "X-Device-Id": device_id}
    return headers, child_user


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


# ---- Group 3: last_active_at 硬切点 ----

@pytest.mark.asyncio
async def test_cross_4am_boundary_creates_new_session(
    api_client_with_eval, auth_headers_child, db_session,
):
    """user_msg.created_at 与 latest.last_active_at 跨 04:00 → 新建 session。（Group 3）"""
    headers, child = auth_headers_child

    fake_payloads = [{"delta": "[reply]"}, {"finish_reason": "stop"}]

    async def fake_astream(initial_state, stream_mode="custom"):
        for p in fake_payloads:
            yield p

    # 写入昨日 03:30 的 session（逻辑日 = 前一日）
    old_sid = uuid.uuid4()
    yesterday = datetime(2026, 5, 10, 3, 30, tzinfo=SHANGHAI)
    old_session = SessionModel(
        id=old_sid, child_user_id=child.id, title="昨日会话",
        last_active_at=yesterday,
    )
    db_session.add(old_session)
    old_msg = Message(
        session_id=old_sid, role=MessageRole.human, content="昨日消息",
        status=MessageStatus.active, created_at=yesterday,
    )
    db_session.add(old_msg)
    await db_session.commit()

    with patch.object(main_graph, "astream", fake_astream):
        body = make_payload(content="今日新消息")
        resp = await api_client_with_eval.post(
            "/api/v1/me/chat/stream", json=body, headers=headers
        )
        assert resp.status_code == 200

        frames = _parse_sse_frames(resp.text)
        new_sid = uuid.UUID(frames[0]["data"]["session_id"])

    # 新 sid ≠ 旧 sid（跨硬切点触发了新建）
    assert new_sid != old_sid, "硬切点应创建新 session"
    new_session = await db_session.get(SessionModel, new_sid)
    assert new_session is not None
    assert new_session.child_user_id == child.id
    # 标题是中文日期格式
    assert "周" in new_session.title and "月" in new_session.title
