"""auth deps 模块 TDD：get_current_account / require_parent / require_child 覆盖。
续期链路走 GET /api/v1/me（app/api/me.py）；
device_changed 走 revoke_token + commit_with_redis 返回 401。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.redis_ops import commit_with_redis, discard_pending_redis_ops
from app.auth.tokens import (
    REDIS_KEY_PREFIX,
    issue_token,
    resolve_token,
    revoke_token,
    token_hash,
)
from app.models.accounts import AuthToken, Family, FamilyMember, User
from app.models.enums import UserRole


# ---- 辅助 fixtures ----

@pytest_asyncio.fixture
async def parent_user(db_session: AsyncSession) -> User:
    """种一个 active parent + family + family_members。"""
    fam = Family()
    db_session.add(fam)
    await db_session.flush()

    user = User(
        family_id=fam.id,
        role=UserRole.parent,
        phone="abcd",
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    db_session.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.parent))
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def child_user(db_session: AsyncSession) -> User:
    """种一个 child + family。"""
    fam = Family()
    db_session.add(fam)
    await db_session.flush()

    user = User(
        family_id=fam.id,
        role=UserRole.child,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    db_session.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.child))
    await db_session.commit()
    return user


def _redis_key(th: str) -> str:
    return f"{REDIS_KEY_PREFIX}{th}"


# ---- get_current_account ----

class TestGetCurrentAccountAuth:
    @pytest.mark.asyncio
    async def test_no_auth_header_returns_401(self, api_client) -> None:
        resp = await api_client.get("/api/v1/me")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_non_bearer_auth_returns_401(self, api_client) -> None:
        resp = await api_client.get("/api/v1/me", headers={"Authorization": "Basic abc123"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(
        self, api_client, db_session: AsyncSession, redis_client
    ) -> None:
        resp = await api_client.get(
            "/api/v1/me",
            headers={"Authorization": "Bearer invalid_token_abc123"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_and_device_returns_account(
        self, api_client, db_session: AsyncSession, redis_client, parent_user: User
    ) -> None:
        token = await issue_token(
            db_session,
            user_id=parent_user.id,
            role=parent_user.role,
            family_id=parent_user.family_id,
            device_id="devA",
        )
        await commit_with_redis(db_session, redis_client)

        resp = await api_client.get(
            "/api/v1/me",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Device-Id": "devA",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "parent"
        assert data["family_id"] == str(parent_user.family_id)


# ---- device_changed 吊销链路 ----

class TestDeviceChanged:
    @pytest.mark.asyncio
    async def test_missing_x_device_id_revokes_token(
        self, api_client, db_session: AsyncSession, redis_client, parent_user: User
    ) -> None:
        token = await issue_token(
            db_session,
            user_id=parent_user.id,
            role=parent_user.role,
            family_id=parent_user.family_id,
            device_id="devB",
        )
        await commit_with_redis(db_session, redis_client)
        th = token_hash(token)

        resp = await api_client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {token}"},  # 缺 X-Device-Id
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "device_changed"

        # DB revoked_at 已写入
        from sqlalchemy import select
        row = (await db_session.execute(
            select(AuthToken.revoked_at).where(AuthToken.token_hash == th)
        )).scalar_one()
        assert row is not None

        # Redis key 已清
        cached = await redis_client.get(_redis_key(th))
        assert cached is None

    @pytest.mark.asyncio
    async def test_device_id_mismatch_revokes_token(
        self, api_client, db_session: AsyncSession, redis_client, parent_user: User
    ) -> None:
        token = await issue_token(
            db_session,
            user_id=parent_user.id,
            role=parent_user.role,
            family_id=parent_user.family_id,
            device_id="devC",
        )
        await commit_with_redis(db_session, redis_client)
        th = token_hash(token)

        resp = await api_client.get(
            "/api/v1/me",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Device-Id": "different_device",  # 不匹配
            },
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "device_changed"

        # DB revoked_at 已写入
        from sqlalchemy import select
        row = (await db_session.execute(
            select(AuthToken.revoked_at).where(AuthToken.token_hash == th)
        )).scalar_one()
        assert row is not None

        # Redis key 已清
        cached = await redis_client.get(_redis_key(th))
        assert cached is None

    @pytest.mark.asyncio
    async def test_after_device_changed_replay_still_401(
        self, api_client, db_session: AsyncSession, redis_client, parent_user: User
    ) -> None:
        token = await issue_token(
            db_session,
            user_id=parent_user.id,
            role=parent_user.role,
            family_id=parent_user.family_id,
            device_id="devD",
        )
        await commit_with_redis(db_session, redis_client)

        # 先用错误 device_id 触发吊销
        await api_client.get(
            "/api/v1/me",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Device-Id": "wrong_device",
            },
        )

        # 再用正确 device_id 重放 —— token 已吊销，Redis miss + DB revoked_at 非空 → 401
        resp = await api_client.get(
            "/api/v1/me",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Device-Id": "devD",
            },
        )
        assert resp.status_code == 401


# ---- role guards ----

class TestRequireParent:
    @pytest.mark.asyncio
    async def test_child_token_returns_403(
        self, api_client, db_session: AsyncSession, redis_client, child_user: User
    ) -> None:
        token = await issue_token(
            db_session,
            user_id=child_user.id,
            role=child_user.role,
            family_id=child_user.family_id,
            device_id="childdev",
            ttl_days=None,
        )
        await commit_with_redis(db_session, redis_client)

        # /api/v1/me 只用 get_current_account，不需要 parent，但 child 可以访问
        # 用一个 parent-only 端点测试 require_parent...
        # 但 Step 4 尚未有 parent-only 端点，这里用 /me 测 require_parent 的替代：
        # 其实 require_parent 的测试应该用 parent-only 端点，这里先跳过，
        # 等 Step 5 的 /children/{id}/revoke-tokens 端点...
        # 临时：mock 一个 require_parent 在路径里用...
        # 最直接：测 require_parent 逻辑在 auth_deps 内部，
        # 用一个 parent-only 端点来验证...
        # 等等，Step 5 才有 parent-only 端点...
        # 临时方案：直接 import require_parent 测
        from app.auth.deps import require_parent
        from app.schemas.accounts import CurrentAccount

        child_account = CurrentAccount(
            id=child_user.id,
            role=UserRole.child,
            family_id=child_user.family_id,
            expires_at=None,
        )
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await require_parent(child_account)
        assert exc_info.value.status_code == 403


class TestRequireChild:
    @pytest.mark.asyncio
    async def test_parent_token_returns_403(self) -> None:
        from app.auth.deps import require_child
        from app.schemas.accounts import CurrentAccount
        import uuid

        parent_account = CurrentAccount(
            id=uuid.uuid4(),
            role=UserRole.parent,
            family_id=uuid.uuid4(),
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await require_child(parent_account)
        assert exc_info.value.status_code == 403


# ---- 每日首次续期 ----

class TestDailyRenewal:
    @pytest.mark.asyncio
    async def test_first_request_of_day_renews_db_expires_at(
        self, api_client, db_session: AsyncSession, redis_client, parent_user: User,
    ) -> None:
        """mock 昨日 → 首次 /me 命中 → DB expires_at 已续 + Redis payload 更新"""
        token = await issue_token(
            db_session,
            user_id=parent_user.id,
            role=parent_user.role,
            family_id=parent_user.family_id,
            device_id="devE",
        )
        await commit_with_redis(db_session, redis_client)
        th = token_hash(token)

        from sqlalchemy import select
        before = (await db_session.execute(
            select(AuthToken.expires_at).where(AuthToken.token_hash == th)
        )).scalar_one()
        assert before is not None

        # mock yesterday for needs_roll to return True
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()

        # patch _today_cst so needs_roll thinks today is yesterday (different from last_rolled_date)
        import app.auth.tokens as tokens_module
        original_today = tokens_module._today_cst

        def fake_today_yesterday():
            return yesterday

        tokens_module._today_cst = fake_today_yesterday

        try:
            resp = await api_client.get(
                "/api/v1/me",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Device-Id": "devE",
                },
            )
        finally:
            tokens_module._today_cst = original_today

        assert resp.status_code == 200

        # DB expires_at 已续（commit_with_redis 后在外层 transaction 可见）
        from sqlalchemy import select
        after = (await db_session.execute(
            select(AuthToken.expires_at).where(AuthToken.token_hash == th)
        )).scalar_one()
        assert after is not None
        assert after > before
        assert after > datetime.now(timezone.utc)

        # Redis payload last_rolled_date 已更新为 today
        cached = await redis_client.get(_redis_key(th))
        assert cached is not None
        import json
        payload = json.loads(cached)
        assert payload["last_rolled_date"] == original_today()

    @pytest.mark.asyncio
    async def test_second_request_same_day_skips_db_update(
        self, api_client, db_session: AsyncSession, redis_client, parent_user: User,
    ) -> None:
        """mock 今日（same day）→ 第二次 /me → DB 不再 UPDATE（只刷 Redis TTL）"""
        token = await issue_token(
            db_session,
            user_id=parent_user.id,
            role=parent_user.role,
            family_id=parent_user.family_id,
            device_id="devF",
        )
        await commit_with_redis(db_session, redis_client)
        th = token_hash(token)

        from sqlalchemy import select
        before = (await db_session.execute(
            select(AuthToken.expires_at).where(AuthToken.token_hash == th)
        )).scalar_one()

        # 先 mock 昨日触发首次续期
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        import app.auth.tokens as tokens_module
        original_today = tokens_module._today_cst

        def fake_today_yesterday():
            return yesterday

        tokens_module._today_cst = fake_today_yesterday
        try:
            resp1 = await api_client.get(
                "/api/v1/me",
                headers={"Authorization": f"Bearer {token}", "X-Device-Id": "devF"},
            )
            assert resp1.status_code == 200
        finally:
            tokens_module._today_cst = original_today

        # 查续期后的值
        after_roll = (await db_session.execute(
            select(AuthToken.expires_at).where(AuthToken.token_hash == th)
        )).scalar_one()

        # 第二次调用（今日，已续过）
        resp2 = await api_client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "devF"},
        )
        assert resp2.status_code == 200

        # DB expires_at 不变
        from sqlalchemy import select
        after2 = (await db_session.execute(
            select(AuthToken.expires_at).where(AuthToken.token_hash == th)
        )).scalar_one()
        assert after2 == after_roll

    @pytest.mark.asyncio
    async def test_child_token_never_renews(
        self, api_client, db_session: AsyncSession, redis_client, child_user: User,
    ) -> None:
        """child token ttl_days=None，跨日 /me → DB expires_at 保持 NULL，无 UPDATE"""
        token = await issue_token(
            db_session,
            user_id=child_user.id,
            role=child_user.role,
            family_id=child_user.family_id,
            device_id="childG",
            ttl_days=None,
        )
        await commit_with_redis(db_session, redis_client)
        th = token_hash(token)

        from sqlalchemy import select
        before = (await db_session.execute(
            select(AuthToken.expires_at).where(AuthToken.token_hash == th)
        )).scalar_one()
        assert before is None

        # mock yesterday
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        import app.auth.tokens as tokens_module
        original_today = tokens_module._today_cst

        def fake_today_yesterday():
            return yesterday

        tokens_module._today_cst = fake_today_yesterday
        try:
            resp = await api_client.get(
                "/api/v1/me",
                headers={"Authorization": f"Bearer {token}", "X-Device-Id": "childG"},
            )
        finally:
            tokens_module._today_cst = original_today

        assert resp.status_code == 200

        # DB expires_at 仍为 NULL
        from sqlalchemy import select
        after = (await db_session.execute(
            select(AuthToken.expires_at).where(AuthToken.token_hash == th)
        )).scalar_one()
        assert after is None
