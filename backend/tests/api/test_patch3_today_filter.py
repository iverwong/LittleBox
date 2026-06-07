"""Group 7：list_sessions 顶层 today_session_id + 过滤今日。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")  # 覆盖 pyproject.toml 的 session 级 loop scope
from app.core.db import get_db
from app.core.enums import UserRole
from app.core.redis import commit_with_redis
from app.core.time import SHANGHAI
from app.domain.auth.tokens import issue_token
from app.domain.chat.models import Session as SessionModel
from app.domain.chat.session_policy import logical_day
from httpx import ASGITransport, AsyncClient

# ---- fixtures ----


@pytest.fixture
async def app(db_session, redis_client):
    from app.core.redis import get_redis
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
async def auth_headers_child(db_session, redis_client, child_user):
    device_id = "test-device-g7"
    token = await issue_token(
        db_session, user_id=child_user.id, role=UserRole.child,
        family_id=child_user.family_id, device_id=device_id, ttl_days=None,
    )
    await commit_with_redis(db_session, redis_client)
    headers = {"Authorization": f"Bearer {token}", "X-Device-Id": device_id}
    return headers, child_user


# ---- Group 7 ----

@pytest.mark.asyncio
async def test_list_sessions_today_filter(api_client, auth_headers_child, db_session):
    """今日有 session 时 today_session_id ≠ null，sessions 不含今日。（Group 7）"""
    headers, child = auth_headers_child

    now = datetime.now(SHANGHAI)
    today = logical_day(now)

    # 创建 2 条 session：sid_old（昨日）+ sid_today（今日）
    yesterday_ts = now - timedelta(days=1)
    sid_old = uuid.uuid4()
    db_session.add(SessionModel(
        id=sid_old, child_user_id=child.id, title="昨日会话",
        status="active", last_active_at=yesterday_ts,
    ))

    sid_today = uuid.uuid4()
    db_session.add(SessionModel(
        id=sid_today, child_user_id=child.id, title="今日会话",
        status="active", last_active_at=now,
    ))
    await db_session.commit()

    resp = await api_client.get("/api/v1/me/sessions", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    # today_session_id 正确
    assert data["today_session_id"] == str(sid_today), (
        f"expected {sid_today}, got {data['today_session_id']}"
    )

    # sessions 数组仅含 sid_old（不含 sid_today）
    session_ids = [s["id"] for s in data["sessions"]]
    assert str(sid_old) in session_ids, "old session should appear"
    assert str(sid_today) not in session_ids, "today session should be filtered out"

    # 仅 1 条历史 session
    assert len(data["sessions"]) == 1


@pytest.mark.asyncio
async def test_list_sessions_no_today(api_client, auth_headers_child, db_session):
    """今日无 session 时 today_session_id == null。（Group 7）"""
    headers, child = auth_headers_child

    # 只创建昨日 session
    yesterday_ts = datetime.now(SHANGHAI) - timedelta(days=1)
    sid_old = uuid.uuid4()
    db_session.add(SessionModel(
        id=sid_old, child_user_id=child.id, title="昨日会话",
        status="active", last_active_at=yesterday_ts,
    ))
    await db_session.commit()

    resp = await api_client.get("/api/v1/me/sessions", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert data["today_session_id"] is None, (
        f"expected null, got {data['today_session_id']}"
    )
    session_ids = [s["id"] for s in data["sessions"]]
    assert str(sid_old) in session_ids
