"""auth login / logout 端点 TDD：Phase A 骨架 → Phase B 实现。"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.password import generate_password, generate_phone, hash_password
from app.auth.tokens import REDIS_KEY_PREFIX, issue_token, revoke_all_active_tokens, token_hash
from app.auth.redis_ops import commit_with_redis
from app.models.accounts import Family, FamilyMember, User
from app.models.enums import UserRole


# ---- 辅助 fixtures ----

@pytest_asyncio.fixture
async def parent_with_password(db_session: AsyncSession) -> tuple[User, str]:
    """种一个 active parent + family + family_members + password_hash。"""
    fam = Family()
    db_session.add(fam)
    await db_session.flush()

    pw = generate_password()
    user = User(
        family_id=fam.id,
        role=UserRole.parent,
        phone=generate_phone(),
        password_hash=hash_password(pw),
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    db_session.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.parent))
    await db_session.commit()
    return user, pw


@pytest_asyncio.fixture
async def inactive_parent(db_session: AsyncSession) -> tuple[User, str]:
    """种一个 inactive parent（is_active=False）。"""
    fam = Family()
    db_session.add(fam)
    await db_session.flush()

    pw = generate_password()
    user = User(
        family_id=fam.id,
        role=UserRole.parent,
        phone=generate_phone(),
        password_hash=hash_password(pw),
        is_active=False,
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.commit()
    return user, pw


@pytest_asyncio.fixture
async def child_with_password(db_session: AsyncSession) -> tuple[User, str]:
    """种一个 child（无 password_hash）。"""
    fam = Family()
    db_session.add(fam)
    await db_session.flush()

    user = User(
        family_id=fam.id,
        role=UserRole.child,
        phone=generate_phone(),
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.commit()
    return user, "unused_password"


def _redis_key(th: str) -> str:
    return f"{REDIS_KEY_PREFIX}{th}"


# ---- Login 端点测试 ----

class TestLoginEndpoint:
    @pytest.mark.asyncio
    async def test_login_happy_path(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """有效 phone + password → 200 + token + AccountOut（无 password_hash/admin_note）。"""
        user, pw = parent_with_password
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": "dev_login_A"},
        )
        assert resp.status_code == 200, resp.json()
        data = resp.json()
        assert "token" in data and data["token"]
        assert data["account"]["id"] == str(user.id)
        assert data["account"]["role"] == "parent"
        assert data["account"]["family_id"] == str(user.family_id)

    @pytest.mark.asyncio
    async def test_login_response_excludes_sensitive_fields(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """response JSON 不含 password_hash / admin_note。"""
        user, pw = parent_with_password
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": "dev_login_B"},
        )
        assert resp.status_code == 200
        text = resp.text
        assert "password_hash" not in text
        assert "admin_note" not in text

    @pytest.mark.asyncio
    async def test_login_device_id_persisted_to_db(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """登录后 auth_tokens 表 device_id 列 == LoginRequest.device_id。"""
        user, pw = parent_with_password
        device_id = "dev_login_C"
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )
        assert resp.status_code == 200
        token = resp.json()["token"]
        th = token_hash(token)

        from sqlalchemy import select
        from app.models.accounts import AuthToken
        row = (await db_session.execute(
            select(AuthToken.device_id).where(AuthToken.token_hash == th)
        )).scalar_one()
        assert row == device_id

    @pytest.mark.asyncio
    async def test_login_device_id_persisted_to_redis(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """登录后 Redis payload.device_id == LoginRequest.device_id。"""
        user, pw = parent_with_password
        device_id = "dev_login_D"
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )
        assert resp.status_code == 200
        token = resp.json()["token"]
        th = token_hash(token)

        import json
        cached = await redis_client.get(_redis_key(th))
        assert cached is not None
        payload = json.loads(cached)
        assert payload["device_id"] == device_id

    @pytest.mark.asyncio
    async def test_login_new_token_usable_immediately(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """新 token + 同一 device_id 立即可用于 GET /api/v1/me。"""
        user, pw = parent_with_password
        device_id = "dev_login_E"
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )
        assert resp.status_code == 200
        token = resp.json()["token"]

        me_resp = await api_client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert me_resp.status_code == 200
        assert me_resp.json()["id"] == str(user.id)

    @pytest.mark.asyncio
    async def test_login_missing_device_id_422(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """LoginRequest 缺 device_id → pydantic 422。"""
        user, pw = parent_with_password
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_login_wrong_password_401(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """错误密码 → 401，消息不区分"账号不存在"vs"密码错"。"""
        user, _pw = parent_with_password
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": "wrongpassword1", "device_id": "dev_login_F"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid credentials"

    @pytest.mark.asyncio
    async def test_login_wrong_phone_401(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """错误 phone → 401，消息与错误密码完全相同。"""
        _user, pw = parent_with_password
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": "zzzz", "password": pw, "device_id": "dev_login_G"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid credentials"

    @pytest.mark.asyncio
    async def test_login_inactive_parent_401(
        self, api_client, db_session: AsyncSession, redis_client, inactive_parent: tuple[User, str],
    ) -> None:
        """is_active=False 的 parent → 401。"""
        user, pw = inactive_parent
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": "dev_login_H"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid credentials"

    @pytest.mark.asyncio
    async def test_login_child_account_401(
        self, api_client, db_session: AsyncSession, redis_client, child_with_password: tuple[User, str],
    ) -> None:
        """child 账号用 phone 来登录 → 401（child 本就没 password_hash）。"""
        user, _pw = child_with_password
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": "anypassword1", "device_id": "dev_login_I"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid credentials"

    @pytest.mark.asyncio
    async def test_login_second_login_new_device_revokes_old(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """同一 parent 第二次登录（新 device_id）→ 老 token 被吊销。"""
        user, pw = parent_with_password
        device_a = "dev_login_J_A"
        device_b = "dev_login_J_B"

        # 第一次登录 dev_A
        resp_a = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_a},
        )
        assert resp_a.status_code == 200
        token_a = resp_a.json()["token"]
        th_a = token_hash(token_a)

        # 第二次登录 dev_B
        resp_b = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_b},
        )
        assert resp_b.status_code == 200
        token_b = resp_b.json()["token"]

        # token_A + dev_A 再请求 → 401（已被 revoke_all_active_tokens 吊销）
        revoked_resp = await api_client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {token_a}", "X-Device-Id": device_a},
        )
        assert revoked_resp.status_code == 401

        # DB revoked_at 已写入
        from sqlalchemy import select
        from app.models.accounts import AuthToken
        revoked_at = (await db_session.execute(
            select(AuthToken.revoked_at).where(AuthToken.token_hash == th_a)
        )).scalar_one()
        assert revoked_at is not None

        # Redis key 已清
        cached = await redis_client.get(_redis_key(th_a))
        assert cached is None

    @pytest.mark.asyncio
    async def test_login_same_device_relogin_revokes_previous(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """同一 parent 同设备复登 → 上一个 token 也被 revoke_all_active_tokens 吊销。"""
        user, pw = parent_with_password
        device_id = "dev_login_K"

        # 第一次登录
        resp1 = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )
        assert resp1.status_code == 200
        token1 = resp1.json()["token"]
        th1 = token_hash(token1)

        # 同设备第二次登录
        resp2 = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )
        assert resp2.status_code == 200
        token2 = resp2.json()["token"]

        # token1 已被 revoke
        revoked_resp = await api_client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {token1}", "X-Device-Id": device_id},
        )
        assert revoked_resp.status_code == 401


# ---- Logout 端点测试 ----

class TestLogoutEndpoint:
    @pytest.mark.asyncio
    async def test_logout_happy_path(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """logout → 204；之后同一 token 请求 → 401。"""
        user, pw = parent_with_password
        device_id = "dev_logout_A"

        # 登录
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )
        assert resp.status_code == 200
        token = resp.json()["token"]
        th = token_hash(token)

        # logout
        logout_resp = await api_client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert logout_resp.status_code == 204

        # 同一 token 再请求 → 401
        me_resp = await api_client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert me_resp.status_code == 401

        # DB revoked_at 已写入
        from sqlalchemy import select
        from app.models.accounts import AuthToken
        revoked_at = (await db_session.execute(
            select(AuthToken.revoked_at).where(AuthToken.token_hash == th)
        )).scalar_one()
        assert revoked_at is not None

        # Redis key 已清
        cached = await redis_client.get(_redis_key(th))
        assert cached is None

    @pytest.mark.asyncio
    async def test_logout_idempotent(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """同一 token 调两次 logout → 第一次 204，第二次 401（token 已吊销）。"""
        user, pw = parent_with_password
        device_id = "dev_logout_B"

        resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )
        assert resp.status_code == 200
        token = resp.json()["token"]

        resp1 = await api_client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert resp1.status_code == 204

        resp2 = await api_client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert resp2.status_code == 401

    @pytest.mark.asyncio
    async def test_logout_requires_parent(
        self, api_client, db_session: AsyncSession, redis_client, child_with_password: tuple[User, str],
    ) -> None:
        """child token 调 /logout → 403。"""
        user, _pw = child_with_password
        device_id = "dev_logout_C"

        # 用 issue_token 直接给 child 造一个 token（child 无法走 login）
        token = await issue_token(
            db_session,
            user_id=user.id,
            role=user.role,
            family_id=user.family_id,
            device_id=device_id,
        )
        await commit_with_redis(db_session, redis_client)

        resp = await api_client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert resp.status_code == 403
