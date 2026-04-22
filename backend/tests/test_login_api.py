"""auth login / logout 端点 TDD：Phase A 骨架 → Phase B 实现。"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.password import generate_phone
from app.auth.redis_ops import commit_with_redis
from app.auth.tokens import REDIS_KEY_PREFIX, issue_token, token_hash
from app.models.accounts import Family, FamilyMember, User
from app.models.enums import UserRole

# ---- C3 · 响应屏蔽辅助函数 ----

_SECRET_KEYWORDS = ("password_hash", "hashed_password", "secret", "token_hash")


def _assert_no_secret_fields(body: dict) -> None:
    """断言响应 body 中不包含敏感字段。"""
    flat_keys = [k for k in body.keys()]
    offenders = [k for k in flat_keys if any(sec in k.lower() for sec in _SECRET_KEYWORDS)]
    assert not offenders, f"Response body contains secret fields: {offenders}"


# ---- 辅助 fixtures ----

# seeded_parent, inactive_parent: conftest.py::seeded_parent / inactive_parent
# child_user: test_child_bind.py::child_user（无 password_hash）


def _redis_key(th: str) -> str:
    return f"{REDIS_KEY_PREFIX}{th}"


# ---- Login 端点测试 ----

class TestLoginEndpoint:
    @pytest.mark.asyncio
    async def test_login_happy_path(
        self, api_client, db_session: AsyncSession, redis_client, seeded_parent: tuple[User, str],
    ) -> None:
        """有效 phone + password → 200 + token + AccountOut（无 password_hash/admin_note）。"""
        user, pw = seeded_parent
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
        _assert_no_secret_fields(data)
        _assert_no_secret_fields(data["account"])

    @pytest.mark.asyncio
    async def test_login_response_excludes_sensitive_fields(
        self, api_client, db_session: AsyncSession, redis_client, seeded_parent: tuple[User, str],
    ) -> None:
        """response JSON 不含 password_hash / admin_note 等敏感字段。"""
        user, pw = seeded_parent
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": "dev_login_B"},
        )
        assert resp.status_code == 200
        data = resp.json()
        _assert_no_secret_fields(data)
        _assert_no_secret_fields(data["account"])

    @pytest.mark.asyncio
    async def test_login_device_id_persisted_to_db(
        self, api_client, db_session: AsyncSession, redis_client, seeded_parent: tuple[User, str],
    ) -> None:
        """登录后 auth_tokens 表 device_id 列 == LoginRequest.device_id。"""
        user, pw = seeded_parent
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
        self, api_client, db_session: AsyncSession, redis_client, seeded_parent: tuple[User, str],
    ) -> None:
        """登录后 Redis payload.device_id == LoginRequest.device_id。"""
        user, pw = seeded_parent
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
        self, api_client, db_session: AsyncSession, redis_client, seeded_parent: tuple[User, str],
    ) -> None:
        """新 token + 同一 device_id 立即可用于 GET /api/v1/me。"""
        user, pw = seeded_parent
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
        self, api_client, db_session: AsyncSession, redis_client, seeded_parent: tuple[User, str],
    ) -> None:
        """LoginRequest 缺 device_id → pydantic 422。"""
        user, pw = seeded_parent
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_login_wrong_password_401(
        self, api_client, db_session: AsyncSession, redis_client, seeded_parent: tuple[User, str],
    ) -> None:
        """错误密码 → 401，消息不区分"账号不存在"vs"密码错"。"""
        user, _pw = seeded_parent
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": "wrongpassword1", "device_id": "dev_login_F"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid credentials"

    @pytest.mark.asyncio
    async def test_login_wrong_phone_401(
        self, api_client, db_session: AsyncSession, redis_client, seeded_parent: tuple[User, str],
    ) -> None:
        """错误 phone → 401，消息与错误密码完全相同。"""
        _user, pw = seeded_parent
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
        self, api_client, db_session: AsyncSession, redis_client, child_user: User,
    ) -> None:
        """child 账号用 phone 来登录 → 401（child 本就没 password_hash）。"""
        user = child_user
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": "anypassword1", "device_id": "dev_login_I"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid credentials"

    @pytest.mark.asyncio
    async def test_login_second_login_new_device_revokes_old(
        self, api_client, db_session: AsyncSession, redis_client, seeded_parent: tuple[User, str],
    ) -> None:
        """同一 parent 第二次登录（新 device_id）→ 老 token 被吊销。"""
        user, pw = seeded_parent
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
        self, api_client, db_session: AsyncSession, redis_client, seeded_parent: tuple[User, str],
    ) -> None:
        """同一 parent 同设备复登 → 上一个 token 也被 revoke_all_active_tokens 吊销。"""
        user, pw = seeded_parent
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


# ---- Login Rate Limit 测试 ----

class TestLoginRateLimit:
    @pytest.mark.asyncio
    async def test_phone_rate_limit_5_failures_6th_429(
        self, api_client, db_session: AsyncSession, redis_client, seeded_parent: tuple[User, str],
    ) -> None:
        """同 phone 连续错密码 5 次 → 第 6 次返 429。"""
        user, _pw = seeded_parent
        device_id = "dev_rate_A"

        # 5 次错密码
        for i in range(5):
            resp = await api_client.post(
                "/api/v1/auth/login",
                json={"phone": user.phone, "password": f"wrongpw{i}", "device_id": device_id},
            )
            assert resp.status_code == 401, f"attempt {i+1}: {resp.status_code}"

        # 第 6 次 → 429
        resp6 = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": "wrongpw_final", "device_id": device_id},
        )
        assert resp6.status_code == 429
        assert resp6.json()["detail"] == "too many attempts; try again later"

    @pytest.mark.asyncio
    async def test_phone_rate_limit_correct_password_also_429_when_limited(
        self, api_client, db_session: AsyncSession, redis_client, seeded_parent: tuple[User, str],
    ) -> None:
        """同 phone 连续错密码 5 次后，即使正确密码也返回 429。"""
        user, pw = seeded_parent
        device_id = "dev_rate_B"

        for i in range(5):
            await api_client.post(
                "/api/v1/auth/login",
                json={"phone": user.phone, "password": f"wrongpw{i}", "device_id": device_id},
            )

        # 正确密码也被 429
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )
        assert resp.status_code == 429

    @pytest.mark.asyncio
    async def test_ip_rate_limit_20_failures_21st_429(
        self, api_client, db_session: AsyncSession, redis_client, seeded_parent: tuple[User, str],
    ) -> None:
        """同 IP（跨 phone）连续 20 次错密码 → 第 21 次返 429。"""
        user, _pw = seeded_parent
        device_id = "dev_rate_C"

        # 用不同 phone 模拟跨账号 IP 级别攻击
        for i in range(20):
            fake_phone = f"99900000{i:04d}"
            resp = await api_client.post(
                "/api/v1/auth/login",
                json={"phone": fake_phone, "password": f"wrongpw{i}", "device_id": device_id},
            )
            assert resp.status_code == 401, f"attempt {i+1}: {resp.status_code}"

        # 第 21 次 → 429
        resp21 = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": "999999990000", "password": "wrongpw_final", "device_id": device_id},
        )
        assert resp21.status_code == 429

    @pytest.mark.asyncio
    async def test_expire_nx_does_not_refresh_ttl(
        self, api_client, db_session: AsyncSession, redis_client, seeded_parent: tuple[User, str],
    ) -> None:
        """连续 INCR 5 次后，TTL 应 ≤ 60 且 ≠ -1（nx=True 不重置过期）。"""
        user, _pw = seeded_parent
        device_id = "dev_rate_D"

        for i in range(5):
            await api_client.post(
                "/api/v1/auth/login",
                json={"phone": user.phone, "password": f"wrongpw{i}", "device_id": device_id},
            )

        phone_key = f"login_fail:phone:{user.phone}"
        ttl = await redis_client.ttl(phone_key)
        assert ttl > 0 and ttl <= 60, f"TTL={ttl}, expected 0 < TTL <= 60 (nx=True)"

    @pytest.mark.asyncio
    async def test_success_clears_counters(
        self, api_client, db_session: AsyncSession, redis_client, seeded_parent: tuple[User, str],
    ) -> None:
        """成功登录后，两个计数 key 被从 Redis 删除。"""
        user, pw = seeded_parent
        device_id = "dev_rate_E"

        # 先触发几次失败
        for i in range(3):
            await api_client.post(
                "/api/v1/auth/login",
                json={"phone": user.phone, "password": f"wrongpw{i}", "device_id": device_id},
            )

        phone_key = f"login_fail:phone:{user.phone}"
        assert await redis_client.get(phone_key) is not None

        # 成功登录
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )
        assert resp.status_code == 200

        # 两个 key 都已清
        await db_session.commit()
        assert await redis_client.get(phone_key) is None

    # ---- A7 · login rate-limit 窗口到期后计数重置 ----

    @pytest.mark.asyncio
    async def test_login_rate_limit_counter_resets_after_window_expiry(
        self, api_client, db_session: AsyncSession, redis_client, rate_limit_parent: tuple[User, str],
    ) -> None:
        """TTL 到期后 key 删除，计数器重置 → 下一个错密码只计 1。"""
        user, pw = rate_limit_parent
        phone = user.phone  # "abcd"

        device_id = "dev_rate_reset"

        # 5 次错密码 → 1-5 都是 401
        for i in range(5):
            resp = await api_client.post(
                "/api/v1/auth/login",
                json={"phone": phone, "password": f"wrongpw{i}", "device_id": device_id},
            )
            assert resp.status_code == 401, f"attempt {i+1}: {resp.status_code}"

        # 第 6 次 → 429
        resp6 = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": phone, "password": "wrongpw_final", "device_id": device_id},
        )
        assert resp6.status_code == 429
        assert resp6.json()["detail"] == "too many attempts; try again later"

        # 模拟 TTL 到期：删掉计数 key
        phone_key = f"login_fail:phone:{phone}"
        # 先查真实 key 名
        ip_keys = await redis_client.keys("login_fail:ip:*")
        ip_key = ip_keys[0] if ip_keys else None

        await redis_client.delete(phone_key)
        if ip_key:
            await redis_client.delete(ip_key)

        # 再打一次错密码 → 401（不是 429），计数器重置为 1
        resp7 = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": phone, "password": "wrongpw_after_reset", "device_id": device_id},
        )
        assert resp7.status_code == 401

        # 验证计数器重置为 1
        count_after = await redis_client.get(phone_key)
        assert count_after == "1"


# ---- Logout 端点测试 ----

class TestLogoutEndpoint:
    @pytest.mark.asyncio
    async def test_logout_happy_path(
        self, api_client, db_session: AsyncSession, redis_client, seeded_parent: tuple[User, str],
    ) -> None:
        """logout → 204；之后同一 token 请求 → 401。"""
        user, pw = seeded_parent
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
        self, api_client, db_session: AsyncSession, redis_client, seeded_parent: tuple[User, str],
    ) -> None:
        """同一 token 调两次 logout → 第一次 204，第二次 401（token 已吊销）。"""
        user, pw = seeded_parent
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
        self, api_client, db_session: AsyncSession, redis_client, child_user: User,
    ) -> None:
        """child token 调 /logout → 403。"""
        user = child_user
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
