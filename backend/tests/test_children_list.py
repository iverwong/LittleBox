"""M4.8 B4 TDD：GET /children 列表。"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

from app.auth.password import hash_password
from app.models.accounts import ChildProfile, Family, FamilyMember, User
from app.models.enums import UserRole


async def _login(api_client, user: User, pw: str, device_id: str = "test_device") -> str:
    login_resp = await api_client.post(
        "/api/v1/auth/login",
        json={"phone": user.phone, "password": pw, "device_id": device_id},
    )
    return login_resp.json()["token"]


async def _make_child(
    api_client,
    parent_token: str,
    nickname: str,
    age: int = 10,
    gender: str = "unknown",
    device_id: str = "test_device",
) -> dict:
    resp = await api_client.post(
        "/api/v1/children",
        json={"nickname": nickname, "age": age, "gender": gender},
        headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": device_id},
    )
    assert resp.status_code == 201, resp.json()
    return resp.json()


class TestListChildrenEmpty:
    """列表为空：parent 尚未创建任何 child。"""

    @pytest.mark.asyncio
    async def test_empty_list(
        self, api_client, seeded_parent: tuple[User, str]
    ) -> None:
        parent, pw = seeded_parent
        token = await _login(api_client, parent, pw)
        resp = await api_client.get(
            "/api/v1/children",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["children"] == []


class TestListChildrenSingle:
    """单 child 场景：未绑定 / 已绑定。"""

    @pytest.mark.asyncio
    async def test_single_unbound(
        self, api_client, seeded_parent: tuple[User, str]
    ) -> None:
        parent, pw = seeded_parent
        token = await _login(api_client, parent, pw)

        child = await _make_child(api_client, token, "小明", age=10, gender="male")

        resp = await api_client.get(
            "/api/v1/children",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 200
        data = resp.json()["children"]
        assert len(data) == 1
        assert data[0]["id"] == child["id"]
        assert data[0]["nickname"] == "小明"
        assert data[0]["is_bound"] is False

    @pytest.mark.asyncio
    async def test_single_bound(
        self, api_client, seeded_parent: tuple[User, str]
    ) -> None:
        parent, pw = seeded_parent
        parent_token = await _login(api_client, parent, pw)

        # parent 创建 child
        child_resp = await api_client.post(
            "/api/v1/children",
            json={"nickname": "小红", "age": 8, "gender": "female"},
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
            json={"device_id": "child_device_abc"},
        )
        assert redeem_resp.status_code == 200

        # GET list → is_bound = True
        resp = await api_client.get(
            "/api/v1/children",
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 200
        data = resp.json()["children"]
        assert len(data) == 1
        assert data[0]["is_bound"] is True


class TestListChildrenMixed:
    """混合绑定态：部分已绑定、部分未绑定。"""

    @pytest.mark.asyncio
    async def test_mixed_bound_and_unbound(
        self, api_client, seeded_parent: tuple[User, str]
    ) -> None:
        parent, pw = seeded_parent
        token = await _login(api_client, parent, pw)

        # 创建 3 个 child
        c1 = await _make_child(api_client, token, "child_1", age=10, gender="unknown")
        c2 = await _make_child(api_client, token, "child_2", age=12, gender="unknown")
        c3 = await _make_child(api_client, token, "child_3", age=14, gender="unknown")

        # c2 绑定
        bind_resp = await api_client.post(
            "/api/v1/bind-tokens",
            json={"child_user_id": str(c2["id"])},
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        bind_token = bind_resp.json()["bind_token"]
        redeem_resp = await api_client.post(
            f"/api/v1/bind-tokens/{bind_token}/redeem",
            json={"device_id": "device_c2"},
        )
        assert redeem_resp.status_code == 200

        resp = await api_client.get(
            "/api/v1/children",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 200
        children = resp.json()["children"]
        assert len(children) == 3

        bound_ids = {c["id"] for c in children if c["is_bound"]}
        unbound_ids = {c["id"] for c in children if not c["is_bound"]}
        assert bound_ids == {c2["id"]}
        assert unbound_ids == {c1["id"], c3["id"]}


class TestListChildrenRevoke:
    """revoke-tokens 回归：revoke 后 is_bound 翻 false。"""

    @pytest.mark.asyncio
    async def test_revoke_is_bound_false(
        self, api_client, seeded_parent: tuple[User, str]
    ) -> None:
        parent, pw = seeded_parent
        token = await _login(api_client, parent, pw)

        # 创建并绑定 child
        child = await _make_child(api_client, token, "to_revoke", age=10, gender="unknown")
        bind_resp = await api_client.post(
            "/api/v1/bind-tokens",
            json={"child_user_id": str(child["id"])},
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        bind_token = bind_resp.json()["bind_token"]
        redeem_resp = await api_client.post(
            f"/api/v1/bind-tokens/{bind_token}/redeem",
            json={"device_id": "revoke_test_device"},
        )
        assert redeem_resp.status_code == 200

        # GET → is_bound=True
        resp1 = await api_client.get(
            "/api/v1/children",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        assert resp1.json()["children"][0]["is_bound"] is True

        # revoke
        revoke_resp = await api_client.post(
            f"/api/v1/children/{child['id']}/revoke-tokens",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        assert revoke_resp.status_code == 204

        # GET → is_bound=False
        resp2 = await api_client.get(
            "/api/v1/children",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        assert resp2.json()["children"][0]["is_bound"] is False


class TestListChildrenOrdering:
    """稳定排序：返回顺序按 (created_at, id)。验证 created_at 升序 + id 兜底。"""

    @pytest.mark.asyncio
    async def test_ordering_by_created_at(
        self, api_client, seeded_parent: tuple[User, str]
    ) -> None:
        parent, pw = seeded_parent
        token = await _login(api_client, parent, pw)

        # 顺序创建 3 个 child，每次间隔 3s 保证 created_at 秒级精度下必然不同
        await _make_child(api_client, token, "first", age=10, gender="unknown")
        await asyncio.sleep(3.0)
        await _make_child(api_client, token, "second", age=11, gender="unknown")
        await asyncio.sleep(3.0)
        await _make_child(api_client, token, "third", age=12, gender="unknown")

        resp = await api_client.get(
            "/api/v1/children",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 200
        children = resp.json()["children"]
        assert len(children) == 3

        # 幂等：二次调用顺序一致（ORDER BY 稳定性验证）
        resp2 = await api_client.get(
            "/api/v1/children",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        children2 = resp2.json()["children"]
        ids1 = [c["id"] for c in children]
        ids2 = [c["id"] for c in children2]
        assert ids1 == ids2, "ordering must be stable across calls"

    @pytest.mark.asyncio
    async def test_ordering_secondary_by_child_profile_id_when_created_at_equal(
        self,
        api_client,
        db_session,
        seeded_parent: tuple[User, str],
    ) -> None:
        """同 created_at 时严格按 ChildProfile.id 升序。"""
        parent, pw = seeded_parent
        token = await _login(api_client, parent, pw)

        # 1. 创建 3 个 child，保留响应里的 child id
        c1 = await _make_child(api_client, token, "first", age=10, gender="unknown")
        c2 = await _make_child(api_client, token, "second", age=11, gender="unknown")
        c3 = await _make_child(api_client, token, "third", age=12, gender="unknown")

        # 2. 直接 UPDATE 3 条 ChildProfile.created_at 为同一时间戳
        fixed_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        child_uuids = [uuid.UUID(c1["id"]), uuid.UUID(c2["id"]), uuid.UUID(c3["id"])]
        await db_session.execute(
            update(ChildProfile)
            .where(ChildProfile.child_user_id.in_(child_uuids))
            .values(created_at=fixed_ts)
        )
        await db_session.commit()

        # 3. 从 DB 读出 3 条 ChildProfile 的 (id, child_user_id)
        result = await db_session.execute(
            select(ChildProfile.id, ChildProfile.child_user_id)
            .where(ChildProfile.child_user_id.in_(child_uuids))
        )
        rows = result.fetchall()
        # 4. expected_order = sorted by ChildProfile.id
        expected_order = [str(row.child_user_id) for row in sorted(rows, key=lambda r: r.id)]

        # 5. GET /children
        resp = await api_client.get(
            "/api/v1/children",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 200
        actual_order = [c["id"] for c in resp.json()["children"]]

        # 6. 断言顺序匹配
        assert actual_order == expected_order, (
            f"expected {expected_order}, got {actual_order}"
        )


class TestListChildrenCrossFamily:
    """跨家族隔离：A 家 parent 看不到 B 家 child。"""

    @pytest.mark.asyncio
    async def test_cross_family_isolation(
        self,
        api_client,
        db_session,
        seeded_parent: tuple[User, str],
    ) -> None:
        parent_a, pw_a = seeded_parent
        token_a = await _login(api_client, parent_a, pw_a)

        # A 家创建一个 child
        child_a = await _make_child(api_client, token_a, "family_a_child", age=10, gender="unknown")

        # 创建 B 家 parent + family
        family_b = Family()
        db_session.add(family_b)
        await db_session.flush()

        parent_b = User(
            family_id=family_b.id,
            role=UserRole.parent,
            phone="99990001",
            is_active=True,
            password_hash=hash_password("TestParent2!"),
        )
        db_session.add(parent_b)
        await db_session.flush()

        db_session.add(FamilyMember(
            family_id=family_b.id,
            user_id=parent_b.id,
            role=UserRole.parent,
            joined_at=datetime.now(timezone.utc),
        ))
        await db_session.commit()

        token_b = await _login(api_client, parent_b, "TestParent2!", device_id="device_b")

        # B 家 GET → 看不到 A 家 child
        resp_b = await api_client.get(
            "/api/v1/children",
            headers={"Authorization": f"Bearer {token_b}", "X-Device-Id": "device_b"},
        )
        assert resp_b.status_code == 200
        children_b = resp_b.json()["children"]
        b_ids = {c["id"] for c in children_b}
        assert child_a["id"] not in b_ids


class TestListChildrenAuth:
    """鉴权边界：401 未登录 / 403 child token。"""

    @pytest.mark.asyncio
    async def test_unauthenticated_401(self, api_client) -> None:
        resp = await api_client.get("/api/v1/children")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_child_token_forbidden(
        self, api_client, seeded_parent: tuple[User, str]
    ) -> None:
        parent, pw = seeded_parent
        parent_token = await _login(api_client, parent, pw)

        # 创建 child 并绑定
        child = await _make_child(
            api_client, parent_token, "child_who_forbidden", age=10, gender="unknown"
        )
        bind_resp = await api_client.post(
            "/api/v1/bind-tokens",
            json={"child_user_id": str(child["id"])},
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": "test_device"},
        )
        bind_token = bind_resp.json()["bind_token"]
        redeem_resp = await api_client.post(
            f"/api/v1/bind-tokens/{bind_token}/redeem",
            json={"device_id": "child_forbidden_device"},
        )
        child_token = redeem_resp.json()["token"]

        # child token → GET /children → 403
        resp = await api_client.get(
            "/api/v1/children",
            headers={
                "Authorization": f"Bearer {child_token}",
                "X-Device-Id": "child_forbidden_device",
            },
        )
        assert resp.status_code == 403
