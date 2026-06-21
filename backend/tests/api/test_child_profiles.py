"""M10 TDD:/api/v1/child-profiles GET / PATCH 契约测试。

覆盖:
- GET happy path(parent 读取本 family 子账号配置)
- PATCH happy path(部分更新)
- child token 调 GET/PATCH → 403
- 跨 family parent 访问 child → 404

测试函数级 docstring 用 Given / When / Then。"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from app.core.enums import Gender, UserRole
from app.domain.accounts.models import (
    ChildProfile,
    Family,
    FamilyMember,
    User,
)
from app.domain.accounts.service import age_to_birth_date
from app.domain.auth.password import generate_password, generate_phone, hash_password
from sqlalchemy.ext.asyncio import AsyncSession


async def _login(api_client, user: User, pw: str, device_id: str = "test_device") -> str:
    login_resp = await api_client.post(
        "/api/v1/auth/login",
        json={"phone": user.phone, "password": pw, "device_id": device_id},
    )
    return login_resp.json()["token"]


async def _create_child(
    api_client,
    parent_token: str,
    *,
    nickname: str = "小明",
    age: int = 10,
    gender: str = "male",
) -> str:
    """通过 POST /children 创建子账号,返回 child_id 字符串。"""
    resp = await api_client.post(
        "/api/v1/children",
        json={"nickname": nickname, "age": age, "gender": gender},
        headers={
            "Authorization": f"Bearer {parent_token}",
            "X-Device-Id": "test_device",
        },
    )
    assert resp.status_code == 201, resp.json()
    return resp.json()["id"]


async def _bind_child(api_client, parent_token: str, child_id: str) -> tuple[str, str]:
    """parent 创建 bind-token,child redeem,返回 (child_token, child_device_id)。"""
    bind_resp = await api_client.post(
        "/api/v1/bind-tokens",
        json={"child_user_id": child_id},
        headers={
            "Authorization": f"Bearer {parent_token}",
            "X-Device-Id": "test_device",
        },
    )
    assert bind_resp.status_code == 201
    bind_token = bind_resp.json()["bind_token"]
    child_device = "child_device_xyz"

    redeem_resp = await api_client.post(
        f"/api/v1/bind-tokens/{bind_token}/redeem",
        json={"device_id": child_device},
    )
    assert redeem_resp.status_code == 200
    return redeem_resp.json()["token"], child_device


@pytest.fixture
async def parent_with_child(api_client, db_session: AsyncSession, seeded_parent):
    """种好的 parent + 已建 child + 已 redeem 的 child token,返回
    (parent, parent_token, child, child_token, child_device)。"""
    parent, pw = seeded_parent
    parent_token = await _login(api_client, parent, pw)
    child_id = await _create_child(api_client, parent_token)
    child_token, child_device = await _bind_child(api_client, parent_token, child_id)

    # 取 child user
    from sqlalchemy import select

    child = (
        await db_session.execute(select(User).where(User.id == uuid.UUID(child_id)))
    ).scalar_one()
    return parent, parent_token, child, child_token, child_device


@pytest.fixture
async def other_family_with_child(db_session: AsyncSession):
    """种一个独立 family + parent + child profile(用于跨 family 访问测试)。"""
    other_fam = Family()
    db_session.add(other_fam)
    await db_session.flush()

    pw = generate_password()
    other_parent = User(
        family_id=other_fam.id,
        role=UserRole.parent,
        phone=generate_phone(),
        password_hash=hash_password(pw),
        is_active=True,
    )
    db_session.add(other_parent)
    await db_session.flush()

    db_session.add(
        FamilyMember(family_id=other_fam.id, user_id=other_parent.id, role=UserRole.parent)
    )

    other_child = User(family_id=other_fam.id, role=UserRole.child, is_active=True)
    db_session.add(other_child)
    await db_session.flush()

    db_session.add(
        FamilyMember(family_id=other_fam.id, user_id=other_child.id, role=UserRole.child)
    )

    db_session.add(
        ChildProfile(
            child_user_id=other_child.id,
            created_by=other_parent.id,
            birth_date=age_to_birth_date(8),
            gender=Gender.female,
            nickname="other_child",
        )
    )
    await db_session.commit()
    return other_fam, other_parent, other_child, pw


class TestGetChildProfile:
    """GET /api/v1/child-profiles/{child_user_id} happy / 鉴权 / 越权。"""

    @pytest.mark.asyncio
    async def test_parent_get_returns_full_fields(
        self, api_client, parent_with_child
    ) -> None:
        """Given parent token + 本 family child
        When GET /api/v1/child-profiles/{id}
        Then 200,返回包含 nickname / gender / birth_date / age / concerns /
            sensitivity / custom_redlines 的全字段。
        """
        _, parent_token, child, _, _ = parent_with_child

        resp = await api_client.get(
            f"/api/v1/child-profiles/{child.id}",
            headers={
                "Authorization": f"Bearer {parent_token}",
                "X-Device-Id": "test_device",
            },
        )
        assert resp.status_code == 200, resp.json()
        data = resp.json()
        assert data["child_user_id"] == str(child.id)
        assert data["nickname"] == "小明"
        assert data["gender"] == "male"
        assert "birth_date" in data
        assert "age" in data
        # 新增字段
        assert "concerns" in data
        assert "sensitivity" in data
        assert "custom_redlines" in data

    @pytest.mark.asyncio
    async def test_unauthenticated_401(self, api_client, parent_with_child) -> None:
        """Given 无 token
        When GET
        Then 401。
        """
        _, _, child, _, _ = parent_with_child
        resp = await api_client.get(f"/api/v1/child-profiles/{child.id}")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_child_token_403(self, api_client, parent_with_child) -> None:
        """Given child token(自身)
        When GET 自己或别人的 child profile
        Then 403(child 无权限访问)。
        """
        _, _, _, child_token, child_device = parent_with_child
        resp = await api_client.get(
            f"/api/v1/child-profiles/{uuid.uuid4()}",
            headers={
                "Authorization": f"Bearer {child_token}",
                "X-Device-Id": child_device,
            },
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_cross_family_parent_404(
        self, api_client, parent_with_child, other_family_with_child
    ) -> None:
        """Given parent A token + family B 的 child
        When GET
        Then 404(不暴露存在性)。
        """
        _, parent_token, _, _, _ = parent_with_child
        _, _, other_child, _ = other_family_with_child

        resp = await api_client.get(
            f"/api/v1/child-profiles/{other_child.id}",
            headers={
                "Authorization": f"Bearer {parent_token}",
                "X-Device-Id": "test_device",
            },
        )
        assert resp.status_code == 404


class TestPatchChildProfile:
    """PATCH /api/v1/child-profiles/{child_user_id} happy / 鉴权 / 越权。"""

    @pytest.mark.asyncio
    async def test_parent_patch_partial_update(
        self, api_client, db_session: AsyncSession, parent_with_child
    ) -> None:
        """Given parent token + 本 family child
        When PATCH {nickname: "新昵称", concerns: "近期考试压力大"}
        Then 200,响应体反映新值,后续 GET 也读到新值。
        """
        _, parent_token, child, _, _ = parent_with_child

        resp = await api_client.patch(
            f"/api/v1/child-profiles/{child.id}",
            json={"nickname": "新昵称", "concerns": "近期考试压力大"},
            headers={
                "Authorization": f"Bearer {parent_token}",
                "X-Device-Id": "test_device",
            },
        )
        assert resp.status_code == 200, resp.json()
        data = resp.json()
        assert data["nickname"] == "新昵称"
        assert data["concerns"] == "近期考试压力大"

        # 再 GET 确认持久化
        get_resp = await api_client.get(
            f"/api/v1/child-profiles/{child.id}",
            headers={
                "Authorization": f"Bearer {parent_token}",
                "X-Device-Id": "test_device",
            },
        )
        assert get_resp.json()["nickname"] == "新昵称"
        assert get_resp.json()["concerns"] == "近期考试压力大"

    @pytest.mark.asyncio
    async def test_parent_patch_sensitivity_replaces(
        self, api_client, parent_with_child
    ) -> None:
        """Given PATCH sensitivity=新 6 维配置
        When PATCH
        Then sensitivity 整体替换并经 SensitivityConfig 规整读回。
        """
        _, parent_token, child, _, _ = parent_with_child

        new_sens = {
            "emotional": 9,
            "social": 2,
            "values": 7,
            "boundaries": 8,
            "academic": 1,
            "lifestyle": 4,
        }
        resp = await api_client.patch(
            f"/api/v1/child-profiles/{child.id}",
            json={"sensitivity": new_sens},
            headers={
                "Authorization": f"Bearer {parent_token}",
                "X-Device-Id": "test_device",
            },
        )
        assert resp.status_code == 200, resp.json()
        assert resp.json()["sensitivity"] == new_sens

    @pytest.mark.asyncio
    async def test_unauthenticated_401(self, api_client, parent_with_child) -> None:
        """Given 无 token
        When PATCH
        Then 401。
        """
        _, _, child, _, _ = parent_with_child
        resp = await api_client.patch(
            f"/api/v1/child-profiles/{child.id}",
            json={"nickname": "x"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_child_token_403(self, api_client, parent_with_child) -> None:
        """Given child token
        When PATCH
        Then 403。
        """
        _, _, child, child_token, child_device = parent_with_child
        resp = await api_client.patch(
            f"/api/v1/child-profiles/{child.id}",
            json={"nickname": "x"},
            headers={
                "Authorization": f"Bearer {child_token}",
                "X-Device-Id": child_device,
            },
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_cross_family_parent_404(
        self, api_client, parent_with_child, other_family_with_child
    ) -> None:
        """Given parent A + family B 的 child
        When PATCH
        Then 404。
        """
        _, parent_token, _, _, _ = parent_with_child
        _, _, other_child, _ = other_family_with_child

        resp = await api_client.patch(
            f"/api/v1/child-profiles/{other_child.id}",
            json={"nickname": "hack"},
            headers={
                "Authorization": f"Bearer {parent_token}",
                "X-Device-Id": "test_device",
            },
        )
        assert resp.status_code == 404


class TestPatchValidation:
    """PATCH 请求体 Pydantic 422 校验。"""

    @pytest.mark.asyncio
    async def test_age_out_of_range_422(
        self, api_client, parent_with_child
    ) -> None:
        """Given PATCH age=2(<3 合法下界)
        When PATCH
        Then 422。
        """
        _, parent_token, child, _, _ = parent_with_child
        resp = await api_client.patch(
            f"/api/v1/child-profiles/{child.id}",
            json={"age": 2},
            headers={
                "Authorization": f"Bearer {parent_token}",
                "X-Device-Id": "test_device",
            },
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_sensitivity_out_of_range_422(
        self, api_client, parent_with_child
    ) -> None:
        """Given PATCH sensitivity.emotional=10(>9)
        When PATCH
        Then 422。
        """
        _, parent_token, child, _, _ = parent_with_child
        resp = await api_client.patch(
            f"/api/v1/child-profiles/{child.id}",
            json={"sensitivity": {"emotional": 10}},
            headers={
                "Authorization": f"Bearer {parent_token}",
                "X-Device-Id": "test_device",
            },
        )
        assert resp.status_code == 422