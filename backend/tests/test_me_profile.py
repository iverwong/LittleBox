"""M4.8 B5 TDD：GET /me/profile 端点。"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounts import FamilyMember, User
from app.models.enums import UserRole


async def _login(api_client, user: User, pw: str, device_id: str = "test_device") -> str:
    login_resp = await api_client.post(
        "/api/v1/auth/login",
        json={"phone": user.phone, "password": pw, "device_id": device_id},
    )
    return login_resp.json()["token"]


class TestGetMyProfileSuccess:
    """child 正常返回 200 + 全字段。"""

    @pytest.mark.asyncio
    async def test_child_returns_full_profile(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        parent, pw = seeded_parent
        parent_token = await _login(api_client, parent, pw)

        # parent 创建 child
        child_resp = await api_client.post(
            "/api/v1/children",
            json={"nickname": "小明", "age": 10, "gender": "male"},
            headers={
                "Authorization": f"Bearer {parent_token}",
                "X-Device-Id": "test_device",
            },
        )
        assert child_resp.status_code == 201
        child_id = child_resp.json()["id"]

        # parent 创建 bind-token 并让 child redeem
        bind_resp = await api_client.post(
            "/api/v1/bind-tokens",
            json={"child_user_id": str(child_id)},
            headers={
                "Authorization": f"Bearer {parent_token}",
                "X-Device-Id": "test_device",
            },
        )
        bind_token = bind_resp.json()["bind_token"]

        redeem_resp = await api_client.post(
            f"/api/v1/bind-tokens/{bind_token}/redeem",
            json={"device_id": "child_device_xyz"},
        )
        assert redeem_resp.status_code == 200
        child_token = redeem_resp.json()["token"]

        # child 调用 GET /me/profile
        resp = await api_client.get(
            "/api/v1/me/profile",
            headers={
                "Authorization": f"Bearer {child_token}",
                "X-Device-Id": "child_device_xyz",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == child_id
        assert data["nickname"] == "小明"
        assert data["gender"] == "male"
        assert "birth_date" in data
        assert data["birth_date"] is not None


class TestGetMyProfileAuth:
    """鉴权边界：未登录 401 / parent 403。"""

    @pytest.mark.asyncio
    async def test_unauthenticated_401(self, api_client) -> None:
        resp = await api_client.get("/api/v1/me/profile")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_parent_token_403(
        self, api_client, seeded_parent: tuple[User, str]
    ) -> None:
        parent, pw = seeded_parent
        token = await _login(api_client, parent, pw)
        resp = await api_client.get(
            "/api/v1/me/profile",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 403


class TestGetMyProfileNotFound:
    """profile 缺失兜底：child User 存在但无 ChildProfile → 404。"""

    @pytest.mark.asyncio
    async def test_profile_missing_404(
        self,
        api_client,
        db_session: AsyncSession,
        seeded_parent: tuple[User, str],
    ) -> None:
        parent, pw = seeded_parent
        parent_token = await _login(api_client, parent, pw)

        # 裸建 child User（不写 ChildProfile）模拟异常分支
        child = User(
            family_id=parent.family_id,
            role=UserRole.child,
            phone=None,
            is_active=True,
        )
        db_session.add(child)
        await db_session.flush()

        db_session.add(FamilyMember(
            family_id=parent.family_id,
            user_id=child.id,
            role=UserRole.child,
            joined_at=date.today(),
        ))
        await db_session.commit()

        # parent 创建 bind-token 让 child 登录（无需 ChildProfile）
        bind_resp = await api_client.post(
            "/api/v1/bind-tokens",
            json={"child_user_id": str(child.id)},
            headers={
                "Authorization": f"Bearer {parent_token}",
                "X-Device-Id": "test_device",
            },
        )
        bind_token = bind_resp.json()["bind_token"]

        redeem_resp = await api_client.post(
            f"/api/v1/bind-tokens/{bind_token}/redeem",
            json={"device_id": "orphan_device"},
        )
        assert redeem_resp.status_code == 200
        child_token = redeem_resp.json()["token"]

        # GET /me/profile → 404（profile 不存在）
        resp = await api_client.get(
            "/api/v1/me/profile",
            headers={
                "Authorization": f"Bearer {child_token}",
                "X-Device-Id": "orphan_device",
            },
        )
        assert resp.status_code == 404
        # 不泄露 user 是否存在等内部信息
        assert "profile not found" in resp.json()["detail"]
