"""M4.8 B3 TDD：POST /children 契约测试。"""
from __future__ import annotations

from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounts import Family, FamilyMember, User
from app.models.enums import UserRole
from app.services.age_converter import age_to_birth_date


async def _login(api_client, user: User, pw: str, device_id: str = "test_device") -> str:
    login_resp = await api_client.post(
        "/api/v1/auth/login",
        json={"phone": user.phone, "password": pw, "device_id": device_id},
    )
    return login_resp.json()["token"]


def make_payload(
    nickname: str = "小明",
    age: int = 10,
    gender: str = "unknown",
) -> dict:
    return {"nickname": nickname, "age": age, "gender": gender}


class TestCreateChildSuccess:
    @pytest.mark.asyncio
    async def test_success(
        self,
        api_client,
        seeded_parent: tuple[User, str],
    ) -> None:
        parent, pw = seeded_parent
        token = await _login(api_client, parent, pw)

        resp = await api_client.post(
            "/api/v1/children",
            json=make_payload(age=10, gender="male"),
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 201, resp.json()
        data = resp.json()
        assert data["nickname"] == "小明"
        assert data["gender"] == "male"
        assert data["is_bound"] is False
        assert "birth_date" in data
        assert data["birth_date"] is not None
        expected_bd = age_to_birth_date(10, date.today())
        assert data["birth_date"] == expected_bd.isoformat()


class TestCreateChildMissingFields:
    @pytest.mark.asyncio
    async def test_missing_nickname(self, api_client, seeded_parent: tuple[User, str]) -> None:
        parent, pw = seeded_parent
        token = await _login(api_client, parent, pw)
        resp = await api_client.post(
            "/api/v1/children",
            json={"age": 10, "gender": "male"},
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_age(self, api_client, seeded_parent: tuple[User, str]) -> None:
        parent, pw = seeded_parent
        token = await _login(api_client, parent, pw)
        resp = await api_client.post(
            "/api/v1/children",
            json={"nickname": "小明", "gender": "female"},
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_gender(self, api_client, seeded_parent: tuple[User, str]) -> None:
        parent, pw = seeded_parent
        token = await _login(api_client, parent, pw)
        resp = await api_client.post(
            "/api/v1/children",
            json={"nickname": "小明", "age": 10},
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 422


class TestCreateChildValidation:
    @pytest.mark.asyncio
    async def test_age_too_low(self, api_client, seeded_parent: tuple[User, str]) -> None:
        parent, pw = seeded_parent
        token = await _login(api_client, parent, pw)
        resp = await api_client.post(
            "/api/v1/children",
            json=make_payload(age=2),
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_age_too_high(self, api_client, seeded_parent: tuple[User, str]) -> None:
        parent, pw = seeded_parent
        token = await _login(api_client, parent, pw)
        resp = await api_client.post(
            "/api/v1/children",
            json=make_payload(age=22),
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_gender_invalid(self, api_client, seeded_parent: tuple[User, str]) -> None:
        parent, pw = seeded_parent
        token = await _login(api_client, parent, pw)
        resp = await api_client.post(
            "/api/v1/children",
            json=make_payload(gender="other"),
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 422


class TestCreateChildQuota:
    @pytest.mark.asyncio
    async def test_quota_boundary_n_minus_1_succeeds(
        self,
        api_client,
        seeded_parent: tuple,
    ) -> None:
        """N=2 时第 3 次 POST 仍 201（确认边界值不误伤）。"""
        parent, pw = seeded_parent
        token = await _login(api_client, parent, pw)

        for i in range(2):
            resp = await api_client.post(
                "/api/v1/children",
                json=make_payload(nickname=f"child{i}"),
                headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
            )
            assert resp.status_code == 201, f"child {i} should succeed: {resp.json()}"

        # 第3个仍成功
        resp = await api_client.post(
            "/api/v1/children",
            json=make_payload(nickname="child3rd"),
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 201, f"3rd child should succeed: {resp.json()}"

    @pytest.mark.asyncio
    async def test_quota_exceeded_returns_409(
        self,
        api_client,
        seeded_parent: tuple,
    ) -> None:
        """建 3 个孩子后第 4 个 → 409 + body {"detail": "child quota exceeded"}。"""
        parent, pw = seeded_parent
        token = await _login(api_client, parent, pw)

        # 建满 3 个
        for i in range(3):
            resp = await api_client.post(
                "/api/v1/children",
                json=make_payload(nickname=f"child{i}"),
                headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
            )
            assert resp.status_code == 201, f"child {i} should succeed: {resp.json()}"

        # 第 4 个被拒绝
        resp = await api_client.post(
            "/api/v1/children",
            json=make_payload(nickname="child4th"),
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 409, f"4th child should be 409, got {resp.status_code}: {resp.json()}"
        assert resp.json() == {"detail": "child quota exceeded"}


class TestCreateChildAuth:
    @pytest.mark.asyncio
    async def test_child_role_forbidden(
        self,
        api_client,
        seeded_parent: tuple[User, str],
    ) -> None:
        """child token → POST /children → 403。"""
        parent, pw = seeded_parent
        parent_token = await _login(api_client, parent, pw)

        # parent 创建 child
        child_resp = await api_client.post(
            "/api/v1/children",
            json={"nickname": "temp", "age": 10, "gender": "unknown"},
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": "test_device"},
        )
        assert child_resp.status_code == 201
        child_id = child_resp.json()["id"]

        # parent 创建 bind-token
        bind_resp = await api_client.post(
            "/api/v1/bind-tokens",
            json={"child_user_id": str(child_id)},
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": "test_device"},
        )
        bind_token = bind_resp.json()["bind_token"]

        # child redeem → child token
        redeem_resp = await api_client.post(
            f"/api/v1/bind-tokens/{bind_token}/redeem",
            json={"device_id": "child_test_device"},
        )
        child_token = redeem_resp.json()["token"]

        # child token → POST /children → 403
        resp = await api_client.post(
            "/api/v1/children",
            json=make_payload(),
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_test_device"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_unauthenticated_401(self, api_client) -> None:
        """未登录 → 401。"""
        resp = await api_client.post("/api/v1/children", json=make_payload())
        assert resp.status_code == 401
