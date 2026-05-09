"""Tests for POST /me/sessions/{id}/stop (Step 9).

Verifies:
- Active running_streams entry → event.set() triggered
- No running_streams entry → 204 best-effort
- Non-existent session → 404
- Other child's session → 403
- Soft-deleted session (status='deleted') → 404 (aligned with Step 7 convention)
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth.redis_ops import commit_with_redis
from app.auth.tokens import issue_token
from app.chat.locks import running_streams
from app.db import get_db
from app.models.accounts import Family, FamilyMember, User
from app.models.chat import Session as SessionModel
from app.models.enums import SessionStatus, UserRole

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def redis_client_with_eval(redis_client):
    """FakeRedis.eval → Lua DEL-if-nonce-match shim (needed for token issuance)."""
    import fakeredis.aioredis

    async def mock_eval(self, script, num_keys, key, nonce_arg):  # noqa: N805
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
    """App fixture with overridden db + redis."""
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
async def api_client(app_with_eval):
    """Async client bound to app_with_eval."""
    transport = ASGITransport(app=app_with_eval)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
async def auth_headers_child(db_session, redis_client_with_eval, child_user):
    """Return (headers, child_user) with a valid child token."""
    device_id = "test-device-stop"
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


@pytest.fixture
async def other_child(db_session):
    """另一个 child（为 403 测试准备）。"""
    fam = Family()
    db_session.add(fam)
    await db_session.flush()

    user = User(
        family_id=fam.id,
        role=UserRole.child,
        phone="0001",
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    db_session.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.child))
    await db_session.commit()
    return user


@pytest.fixture
async def active_session(db_session, child_user):
    """Create an active session owned by child_user."""
    session = SessionModel(
        child_user_id=child_user.id,
        title="test session",
        status=SessionStatus.active,
    )
    db_session.add(session)
    await db_session.commit()
    return str(session.id)


@pytest.fixture
async def other_session(db_session, other_child):
    """Create an active session owned by other_child (for 403 test)."""
    session = SessionModel(
        child_user_id=other_child.id,
        title="other session",
        status=SessionStatus.active,
    )
    db_session.add(session)
    await db_session.commit()
    return str(session.id)


# ---------------------------------------------------------------------------
# S1: Active running_streams → event.set() triggered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_active_stream(api_client, auth_headers_child, active_session):
    """running_streams 有对应 entry → 调 stop 后 event.is_set() == True + 204."""
    headers, child = auth_headers_child
    sid = active_session

    # 注入真 asyncio.Event（关注点4：不 mock，用真实实例）
    event = asyncio.Event()
    running_streams[sid] = event

    resp = await api_client.post(f"/api/v1/me/sessions/{sid}/stop", headers=headers)
    assert resp.status_code == 204, f"Expected 204, got {resp.status_code}: {resp.text}"

    # event 已被 set（generator 下次 yield 前会检测到并退出）
    assert event.is_set(), "running_streams event was not set by stop endpoint"

    # 清理（generator finally 块会做，但本步测试不启动 generator）
    running_streams.pop(sid, None)


# ---------------------------------------------------------------------------
# S2: No running_streams entry → 204 best-effort
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_best_effort(api_client, auth_headers_child, active_session):
    """running_streams 无对应 entry → 仍返回 204 best-effort。"""
    headers, child = auth_headers_child
    sid = active_session

    # 确保 running_streams 没有此 sid
    running_streams.pop(sid, None)

    resp = await api_client.post(f"/api/v1/me/sessions/{sid}/stop", headers=headers)
    assert resp.status_code == 204, f"Expected 204, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# S3: Non-existent session → 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_session_not_found(api_client, auth_headers_child):
    """不存在的 sid → 404 SessionNotFound。"""
    headers, child = auth_headers_child
    fake_sid = "00000000-0000-0000-0000-000000000000"

    resp = await api_client.post(f"/api/v1/me/sessions/{fake_sid}/stop", headers=headers)
    assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# S4: Other child's session → 403 (顺序：先 404 判不存在，再 403 判归属)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_session_forbidden(api_client, auth_headers_child, other_session):
    """其他 child 的 session → 403 SessionForbidden。

    关注点2：先 SELECT 判 session 存在（404），再判 child_user_id 不匹配（403）。
    Setup：other_session 属于 other_child，用 child_user 的 token 调 stop。
    """
    headers, child = auth_headers_child
    sid = other_session

    resp = await api_client.post(f"/api/v1/me/sessions/{sid}/stop", headers=headers)
    assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# S5: Soft-deleted session → 404 (与 Step 7 约定一致)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_deleted_session(api_client, auth_headers_child, active_session, db_session):
    """软删后调 stop → 404（status='deleted' 的 session 对 stop 不可见）。"""
    headers, child = auth_headers_child
    sid = active_session

    # 先软删
    session = await db_session.get(SessionModel, sid)
    session.status = SessionStatus.deleted
    await db_session.commit()

    # 再调 stop → 404
    resp = await api_client.post(f"/api/v1/me/sessions/{sid}/stop", headers=headers)
    assert resp.status_code == 404, (
        f"Expected 404 for soft-deleted session, got {resp.status_code}: {resp.text}"
    )
