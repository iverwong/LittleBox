"""M4 完整鉴权流程 E2E（M6-patch 重写）：ASGI in-process + conftest fixture。

测试隔离铁律（M6-patch）：
- 不调 subprocess / Popen
- 不调 httpx.Client(base_url="http://localhost:8000")
- 不建 redis.Redis(host="redis", ...) 真连
- 不调 flushdb()
- 所有 DB / Redis / HTTP 经 conftest fixture

用例顺序（14 步）：
1. create_parent CLI → phone + password
2. login → parent token
3. /me (parent token) → 200
4. POST /children → child_id
5. POST /bind-tokens → bind_token
6. POST /bind-tokens/{bind_token}/redeem → child token
7. /me (child token) → 200
8. child token 调用 parent-only 端点 → 403
9. logout (parent token) → 204
10. 老 parent token → 401
11. re-login → 新 parent token
12. reset_parent_password CLI
13. reset 前旧 parent token → 401
14. 新密码 login → 200
"""

from __future__ import annotations

import pytest

from app.scripts.create_parent import _create_parent
from app.scripts.reset_parent_password import _reset_password


class TestFullAuthFlow:
    """完整 M4 流程 E2E（ASGI in-process）。"""

    @pytest.mark.asyncio
    async def test_e2e_full_auth_flow(
        self,
        db_session,
        redis_client,
        api_client,
    ) -> None:
        """14 步串联：CLI 建账号 → login → CRUD → bind → redeem → logout → reset。"""

        # ── 1. create_parent ────────────────────────────────────────────────
        info = await _create_parent(db_session, redis_client, note="e2e-test parent")
        phone = info.phone
        password = info.plain_password
        assert len(phone) == 4
        assert len(password) == 8

        # ── 2. login → parent token ──────────────────────────────────────────
        login_resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": phone, "password": password, "device_id": "parent-e2e"},
        )
        assert login_resp.status_code == 200, login_resp.text
        parent_token = login_resp.json()["token"]
        assert login_resp.json()["account"]["role"] == "parent"
        assert login_resp.json()["account"]["phone"] == phone

        # ── 3. /me (parent token) ────────────────────────────────────────────
        parent_headers = {
            "Authorization": f"Bearer {parent_token}",
            "X-Device-Id": "parent-e2e",
        }
        me_resp = await api_client.get("/api/v1/me", headers=parent_headers)
        assert me_resp.status_code == 200, me_resp.text
        assert me_resp.json()["role"] == "parent"

        # ── 4. POST /children ────────────────────────────────────────────────
        child_resp = await api_client.post(
            "/api/v1/children",
            headers=parent_headers,
            json={"nickname": "e2e-child", "age": 10, "gender": "unknown"},
        )
        assert child_resp.status_code == 201, child_resp.text
        child_id = child_resp.json()["id"]

        # ── 5. POST /bind-tokens ─────────────────────────────────────────────
        bind_resp = await api_client.post(
            "/api/v1/bind-tokens",
            headers=parent_headers,
            json={"child_user_id": child_id},
        )
        assert bind_resp.status_code == 201, bind_resp.text
        bind_token = bind_resp.json()["bind_token"]
        assert len(bind_token) > 16

        # ── 6. redeem bind_token → child token ───────────────────────────────
        redeem_resp = await api_client.post(
            f"/api/v1/bind-tokens/{bind_token}/redeem",
            json={"device_id": "child-e2e"},
        )
        assert redeem_resp.status_code == 200, redeem_resp.text
        child_token = redeem_resp.json()["token"]
        child_account = redeem_resp.json()["account"]
        assert child_account["role"] == "child"
        assert child_account["id"] == child_id

        # ── 7. /me (child token) ─────────────────────────────────────────────
        child_headers = {
            "Authorization": f"Bearer {child_token}",
            "X-Device-Id": "child-e2e",
        }
        child_me_resp = await api_client.get("/api/v1/me", headers=child_headers)
        assert child_me_resp.status_code == 200, child_me_resp.text
        assert child_me_resp.json()["role"] == "child"

        # ── 8. child token → parent-only 端点 → 403 ─────────────────────────
        child_forbidden_resp = await api_client.post(
            "/api/v1/bind-tokens",
            headers=child_headers,
            json={"child_user_id": child_id},
        )
        assert child_forbidden_resp.status_code == 403, (
            f"expected 403, got {child_forbidden_resp.status_code}"
        )

        # ── 9. logout (parent token) ─────────────────────────────────────────
        logout_resp = await api_client.post(
            "/api/v1/auth/logout",
            headers=parent_headers,
        )
        assert logout_resp.status_code == 204, logout_resp.text

        # ── 10. 老 parent token → 401 ─────────────────────────────────────────
        old_token_resp = await api_client.get(
            "/api/v1/me",
            headers=parent_headers,
        )
        assert old_token_resp.status_code == 401

        # ── 11. re-login 拿新 parent token ────────────────────────────────────
        login2_resp = await api_client.post(
            "/api/v1/auth/login",
            json={
                "phone": phone,
                "password": password,
                "device_id": "parent-e2e-2",
            },
        )
        assert login2_resp.status_code == 200, login2_resp.text
        new_parent_token = login2_resp.json()["token"]
        assert new_parent_token != parent_token

        # ── 12. reset_parent_password ─────────────────────────────────────────
        result = await _reset_password(db_session, redis_client, phone=phone)
        new_password = result.plain_password

        # ── 13. reset 前旧 parent token → 401 ─────────────────────────────────
        old_after_reset_resp = await api_client.get(
            "/api/v1/me",
            headers={
                "Authorization": f"Bearer {new_parent_token}",
                "X-Device-Id": "parent-e2e-2",
            },
        )
        assert old_after_reset_resp.status_code == 401, (
            f"token should be invalidated by reset_password; "
            f"got {old_after_reset_resp.status_code}"
        )

        # ── 14. 新密码 login → 200 ────────────────────────────────────────────
        login3_resp = await api_client.post(
            "/api/v1/auth/login",
            json={
                "phone": phone,
                "password": new_password,
                "device_id": "parent-e2e-3",
            },
        )
        assert login3_resp.status_code == 200, login3_resp.text
        assert login3_resp.json()["token"]
