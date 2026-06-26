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
from app.core.time import same_natural_day
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
    # 新策略:自然日为单位(last_create_at vs now)。now 与 sid_today 同日 → 不切。
    # sid_old 的 created_at 在昨日(同 last_active_at)→ 跨日 → 切。

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
        created_at=yesterday_ts,
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


# ---- Group 7 扩展:跨自然日 / R1 同日不切 / R3' 短宽限 ----


@pytest.mark.asyncio
async def test_list_sessions_cross_natural_day_boundary(
    api_client, auth_headers_child, db_session, monkeypatch
):
    """跨自然日 + 大 gap:list_sessions 的 today_session_id 与 chat/stream 一致 → 切 → None。

    新策略 R2: 跨日 + gap > 30min → 切。
    场景:session create_at=T-1 23:30, last_active=T-1 23:30, now=T0 02:00(gap 2.5h) → 切。
    """
    from app.api import me as me_module

    headers, child = auth_headers_child
    fake_now = datetime(2026, 6, 8, 2, 0, 0, tzinfo=SHANGHAI)
    monkeypatch.setattr(me_module, "now_shanghai", lambda: fake_now)

    yesterday_create = fake_now.replace(day=7, hour=23, minute=30)
    sid = uuid.uuid4()
    db_session.add(SessionModel(
        id=sid, child_user_id=child.id, title="昨日会话",
        status="active", last_active_at=yesterday_create,
        created_at=yesterday_create,
    ))
    await db_session.commit()

    resp = await api_client.get("/api/v1/me/sessions", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert data["today_session_id"] is None, (
        f"跨自然日 + gap 2.5h → today_session_id 应为 None, got {data['today_session_id']}"
    )


@pytest.mark.asyncio
async def test_list_sessions_cross_day_short_grace_keeps(
    api_client, auth_headers_child, db_session, monkeypatch
):
    """跨日 + gap ≤ 30min + now < 04:00:不切,保持 today_sid(R3' 宽限)。

    场景:create=T-1 23:55, last_active=T0 00:05(gap 10min), now=T0 00:05 → R3' 不切。
    """
    from app.api import me as me_module

    headers, child = auth_headers_child
    fake_now = datetime(2026, 6, 8, 0, 5, 0, tzinfo=SHANGHAI)
    monkeypatch.setattr(me_module, "now_shanghai", lambda: fake_now)

    yesterday_create = fake_now.replace(hour=23, minute=55) - timedelta(days=1)
    active = fake_now  # 00:05 (gap 10 min)
    sid = uuid.uuid4()
    db_session.add(SessionModel(
        id=sid, child_user_id=child.id, title="刚跨过来",
        status="active", last_active_at=active,
        created_at=yesterday_create,
    ))
    await db_session.commit()

    resp = await api_client.get("/api/v1/me/sessions", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert data["today_session_id"] == str(sid), (
        f"跨日 30min 宽限内 + now < 04:00 → today_session_id 应保持 {sid}, got {data['today_session_id']}"
    )


@pytest.mark.asyncio
async def test_list_sessions_within_day_never_switches(
    api_client, auth_headers_child, db_session, monkeypatch
):
    """同自然日任意 gap → 不切(R1)。

    场景:create=today 13:00, last_active=today 13:00, now=today 22:00(gap 9h) → 不切。
    """
    from app.api import me as me_module

    headers, child = auth_headers_child
    fake_now = datetime(2026, 6, 8, 22, 0, 0, tzinfo=SHANGHAI)
    monkeypatch.setattr(me_module, "now_shanghai", lambda: fake_now)

    morning = fake_now.replace(hour=13, minute=0)
    sid = uuid.uuid4()
    db_session.add(SessionModel(
        id=sid, child_user_id=child.id, title="白天会话",
        status="active", last_active_at=morning,
        created_at=morning,
    ))
    await db_session.commit()

    resp = await api_client.get("/api/v1/me/sessions", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert data["today_session_id"] == str(sid), (
        f"同日 9h gap → today_session_id 应保持 {sid}, got {data['today_session_id']}"
    )
