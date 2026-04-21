"""child 创建 + QR bind + redeem 端点 TDD：Phase A 骨架。"""
from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.bind import BIND_KEY_PREFIX, BIND_RESULT_KEY_PREFIX, issue_bind_token
from app.auth.password import generate_password, generate_phone, hash_password
from app.auth.redis_ops import commit_with_redis
from app.auth.tokens import REDIS_KEY_PREFIX, issue_token
from app.models.accounts import AuthToken, ChildProfile, Family, FamilyMember, User
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
async def child_user(db_session: AsyncSession) -> User:
    """种一个 child + family（无 password_hash）。"""
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

    db_session.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.child))
    await db_session.commit()
    return user


def _redis_key(th: str) -> str:
    return f"{REDIS_KEY_PREFIX}{th}"


# ---- 核心 7 条 ----

class TestCreateChild:
    @pytest.mark.asyncio
    async def test_parent_creates_child_success(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """parent 登录后 POST /api/v1/children → 201 + child AccountOut。"""
        user, pw = parent_with_password
        device_id = "dev_child_A"

        login_resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["token"]

        resp = await api_client.post(
            "/api/v1/children",
            json={"nickname": "小明"},
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert resp.status_code == 201, resp.json()
        data = resp.json()
        assert data["role"] == "child"
        assert data["family_id"] == str(user.family_id)
        assert data["id"]  # UUID 格式

    @pytest.mark.asyncio
    async def test_child_cannot_call_create_child(
        self, api_client, db_session: AsyncSession, redis_client, child_user: User,
    ) -> None:
        """child token 调 POST /children → 403。"""
        device_id = "dev_child_B"
        token = await issue_token(
            db_session,
            user_id=child_user.id,
            role=child_user.role,
            family_id=child_user.family_id,
            device_id=device_id,
            ttl_days=None,
        )
        await commit_with_redis(db_session, redis_client)

        resp = await api_client.post(
            "/api/v1/children",
            json={},
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert resp.status_code == 403


class TestBindToken:
    @pytest.mark.asyncio
    async def test_parent_generates_bind_token_same_family(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """parent 给同 family child 生成 bind_token → 200 + bind_token。"""
        user, pw = parent_with_password
        device_id = "dev_bind_A"

        login_resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["token"]

        # 先创建一个 child
        child_resp = await api_client.post(
            "/api/v1/children",
            json={"nickname": "小红"},
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert child_resp.status_code == 201
        child_id = child_resp.json()["id"]

        # 生成 bind_token
        bind_resp = await api_client.post(
            f"/api/v1/children/{child_id}/bind-token",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert bind_resp.status_code == 200
        assert bind_resp.json()["bind_token"]
        assert bind_resp.json()["expires_in_seconds"] == 300

    @pytest.mark.asyncio
    async def test_generate_bind_token_wrong_family_404(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """parent A 给 family B 的 child 生成 bind_token → 404（不泄漏 child 是否存在）。"""
        user, pw = parent_with_password
        device_id = "dev_bind_B"

        login_resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["token"]

        # 造一个属于别人的 child（不同 family）
        other_fam = Family()
        db_session.add(other_fam)
        await db_session.flush()

        other_child = User(
            family_id=other_fam.id,
            role=UserRole.child,
            is_active=True,
        )
        db_session.add(other_child)
        await db_session.commit()

        bind_resp = await api_client.post(
            f"/api/v1/children/{other_child.id}/bind-token",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert bind_resp.status_code == 404


class TestRedeemBindToken:
    @pytest.mark.asyncio
    async def test_redeem_success_child_token_never_expires(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """redeem 成功 → child 拿到 expires_at IS NULL 的永久 token。"""
        user, pw = parent_with_password
        device_id = "dev_redeem_A"

        login_resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["token"]

        # 创建 child
        child_resp = await api_client.post(
            "/api/v1/children",
            json={},
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert child_resp.status_code == 201
        child_id = child_resp.json()["id"]

        # 生成 bind_token
        bind_resp = await api_client.post(
            f"/api/v1/children/{child_id}/bind-token",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert bind_resp.status_code == 200
        bind_token = bind_resp.json()["bind_token"]

        # redeem
        redeem_resp = await api_client.post(
            "/api/v1/auth/redeem-bind-token",
            json={"bind_token": bind_token, "device_id": "child_dev_redeem"},
        )
        assert redeem_resp.status_code == 200, redeem_resp.json()
        data = redeem_resp.json()
        assert data["token"]
        assert data["account"]["role"] == "child"
        assert data["account"]["id"] == child_id

        # token 可立即使用
        child_token = data["token"]
        me_resp = await api_client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_dev_redeem"},
        )
        assert me_resp.status_code == 200

    @pytest.mark.asyncio
    async def test_redeem_same_token_twice_fails(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """同一 bind_token 重复 redeem → 400 或 404。"""
        user, pw = parent_with_password
        device_id = "dev_redeem_B"

        login_resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["token"]

        child_resp = await api_client.post(
            "/api/v1/children",
            json={},
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert child_resp.status_code == 201
        child_id = child_resp.json()["id"]

        bind_resp = await api_client.post(
            f"/api/v1/children/{child_id}/bind-token",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        bind_token = bind_resp.json()["bind_token"]

        # 第一次 redeem
        await api_client.post(
            "/api/v1/auth/redeem-bind-token",
            json={"bind_token": bind_token, "device_id": "child_dev_first"},
        )

        # 第二次 redeem
        resp2 = await api_client.post(
            "/api/v1/auth/redeem-bind-token",
            json={"bind_token": bind_token, "device_id": "child_dev_second"},
        )
        assert resp2.status_code in (400, 404)

    # ---- A1 · bind_token 过期后 redeem 失败 ----

    @pytest.mark.asyncio
    async def test_redeem_bind_token_returns_400_when_bind_key_expired(
        self,
        api_client,
        db_session: AsyncSession,
        redis_client,
        parent_with_password: tuple[User, str],
        child_user: User,
    ) -> None:
        """bind_token TTL 到期后 redeem → 400 + 无 DB/Redis 副作用。"""
        parent_user, _pw = parent_with_password

        # 直接写 Redis（不走 API），模拟 parent 已生成 bind_token
        token = await issue_bind_token(
            redis_client,
            parent_user_id=parent_user.id,
            child_user_id=child_user.id,
        )

        # 模拟 TTL 到期
        await redis_client.delete(f"{BIND_KEY_PREFIX}{token}")

        # redeem 应被拒绝
        resp = await api_client.post(
            "/api/v1/auth/redeem-bind-token",
            json={"bind_token": token, "device_id": "dev1"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "bind token invalid or expired"

        # 无 auth_tokens 写入
        result = await db_session.execute(
            select(func.count()).select_from(AuthToken).where(AuthToken.user_id == child_user.id)
        )
        assert result.scalar_one() == 0

        # 无 bind_result 残留
        assert await redis_client.get(f"{BIND_RESULT_KEY_PREFIX}{token}") is None

    # ---- A2 · 下线→重绑闭环 ----

    @pytest.mark.asyncio
    async def test_rebind_after_revoke_reuses_child_profile_and_revokes_old_token(
        self,
        api_client,
        db_session: AsyncSession,
        redis_client,
        parent_with_password: tuple[User, str],
    ) -> None:
        """child 被 revoke 后重新绑定 → child_profile 不新建，old token 被吊销。"""
        parent_user, _pw = parent_with_password
        device_id = "dev_rebind"

        # parent 登录
        login_resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": parent_user.phone, "password": _pw, "device_id": device_id},
        )
        assert login_resp.status_code == 200
        parent_token = login_resp.json()["token"]

        # 创建 child
        child_resp = await api_client.post(
            "/api/v1/children",
            json={},
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": device_id},
        )
        assert child_resp.status_code == 201
        child_id = child_resp.json()["id"]

        # 第一轮：绑定
        bind_resp_a = await api_client.post(
            f"/api/v1/children/{child_id}/bind-token",
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": device_id},
        )
        token_a = bind_resp_a.json()["bind_token"]
        redeem_resp_a = await api_client.post(
            "/api/v1/auth/redeem-bind-token",
            json={"bind_token": token_a, "device_id": "child_dev_A"},
        )
        child_token_1 = redeem_resp_a.json()["token"]

        # parent 下线 child
        await api_client.post(
            f"/api/v1/children/{child_id}/revoke-tokens",
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": device_id},
        )

        # child_token_1 已失效
        me_resp = await api_client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {child_token_1}", "X-Device-Id": "child_dev_A"},
        )
        assert me_resp.status_code == 401

        # 第二轮：重新绑定
        bind_resp_b = await api_client.post(
            f"/api/v1/children/{child_id}/bind-token",
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": device_id},
        )
        token_b = bind_resp_b.json()["bind_token"]
        redeem_resp_b = await api_client.post(
            "/api/v1/auth/redeem-bind-token",
            json={"bind_token": token_b, "device_id": "child_dev_B"},
        )
        child_token_2 = redeem_resp_b.json()["token"]

        # child_profile 数量仍为 1（不新建）
        from app.models.accounts import ChildProfile
        result = await db_session.execute(
            select(func.count()).select_from(ChildProfile).where(ChildProfile.child_user_id == uuid.UUID(child_id))
        )
        assert result.scalar_one() == 1

        # auth_tokens 总数 2，其中 revoked_at IS NULL 的有 1 行
        from app.models.accounts import AuthToken
        all_tokens_result = await db_session.execute(
            select(func.count()).select_from(AuthToken).where(AuthToken.user_id == uuid.UUID(child_id))
        )
        assert all_tokens_result.scalar_one() == 2

        active_tokens_result = await db_session.execute(
            select(func.count())
            .select_from(AuthToken)
            .where(AuthToken.user_id == uuid.UUID(child_id), AuthToken.revoked_at.is_(None))
        )
        assert active_tokens_result.scalar_one() == 1

        # 两条 bind_result 都存在
        result_a = await redis_client.get(f"{BIND_RESULT_KEY_PREFIX}{token_a}")
        result_b = await redis_client.get(f"{BIND_RESULT_KEY_PREFIX}{token_b}")
        assert result_a is not None
        assert result_b is not None
        import json

        bound_a = json.loads(result_a)
        bound_b = json.loads(result_b)
        assert bound_a["child_user_id"] == child_id
        assert bound_b["child_user_id"] == child_id

    # ---- A3 · bind status 过期未扫 → 404 ----

    @pytest.mark.asyncio
    async def test_bind_status_404_when_expired_and_never_scanned(
        self,
        api_client,
        db_session: AsyncSession,
        redis_client,
        parent_with_password: tuple[User, str],
        child_user: User,
    ) -> None:
        """bind_token TTL 到期且从未被扫描 → status 返回 404。"""
        parent_user, _pw = parent_with_password

        token = await issue_bind_token(
            redis_client,
            parent_user_id=parent_user.id,
            child_user_id=child_user.id,
        )

        # 模拟 TTL 到期
        await redis_client.delete(f"{BIND_KEY_PREFIX}{token}")

        resp = await api_client.get(f"/api/v1/bind-tokens/{token}/status")
        assert resp.status_code == 404

    # ---- A4 · bind status 过期已扫 → 仍 bound ----

    @pytest.mark.asyncio
    async def test_bind_status_bound_when_bind_key_expired_but_result_alive(
        self,
        api_client,
        db_session: AsyncSession,
        redis_client,
        parent_with_password: tuple[User, str],
    ) -> None:
        """bind_key 已过期但 bind_result 仍有效 → status 返回 bound。"""
        parent_user, _pw = parent_with_password
        device_id = "dev_status_bound"

        # 走完全部 issue + redeem 流程
        login_resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": parent_user.phone, "password": _pw, "device_id": device_id},
        )
        parent_token = login_resp.json()["token"]

        child_resp = await api_client.post(
            "/api/v1/children",
            json={},
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": device_id},
        )
        child_id = child_resp.json()["id"]

        bind_resp = await api_client.post(
            f"/api/v1/children/{child_id}/bind-token",
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": device_id},
        )
        token = bind_resp.json()["bind_token"]

        await api_client.post(
            "/api/v1/auth/redeem-bind-token",
            json={"bind_token": token, "device_id": "child_dev_bound"},
        )

        # bind_key 到期但 bind_result 存活
        await redis_client.delete(f"{BIND_KEY_PREFIX}{token}")
        assert await redis_client.get(f"{BIND_RESULT_KEY_PREFIX}{token}") is not None

        resp = await api_client.get(f"/api/v1/bind-tokens/{token}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "bound"
        assert data["child_user_id"] == child_id
        from datetime import datetime

        assert datetime.fromisoformat(data["bound_at"]) is not None

    # ---- A5 · status 端点零 DB 依赖（签名 + 运行时） ----

    @pytest.mark.asyncio
    async def test_bind_status_endpoint_signature_has_no_async_session_param(
        self,
    ) -> None:
        """get_bind_token_status 签名中不含 AsyncSession（注解级展开检查）。"""
        import typing

        from sqlalchemy.ext.asyncio import AsyncSession

        from app.api.children import get_bind_token_status

        def _annotation_contains_type(annotation: object, target: object) -> bool:
            """递归检查某个类型是否出现在注解树的任意深度。"""
            if annotation is target:
                return True
            origin = typing.get_origin(annotation)
            if origin is None:
                return False
            return any(
                _annotation_contains_type(arg, target)
                for arg in typing.get_args(annotation)
            )

        hints = typing.get_type_hints(get_bind_token_status, include_extras=True)
        offenders: list[str] = []
        for name, hint in hints.items():
            if name == "return":
                continue
            if _annotation_contains_type(hint, AsyncSession):
                offenders.append(f"{name}: {hint!r}")
        assert not offenders, (
            "get_bind_token_status 不应依赖 AsyncSession，"
            f"但以下参数注解中发现了它：{', '.join(offenders)}"
        )

    @pytest.mark.asyncio
    async def test_bind_status_endpoint_does_not_invoke_get_db(
        self,
        api_client,
        db_session: AsyncSession,
        redis_client,
        parent_with_password: tuple[User, str],
    ) -> None:
        """get_bind_token_status 运行时调 get_db → 覆盖并验证不触发。"""
        from app.db import get_db

        parent_user, _pw = parent_with_password
        device_id = "dev_status_nodb"

        login_resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": parent_user.phone, "password": _pw, "device_id": device_id},
        )
        parent_token = login_resp.json()["token"]

        child_resp = await api_client.post(
            "/api/v1/children",
            json={},
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": device_id},
        )
        child_id = child_resp.json()["id"]

        bind_resp = await api_client.post(
            f"/api/v1/children/{child_id}/bind-token",
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": device_id},
        )
        token = bind_resp.json()["bind_token"]

        await api_client.post(
            "/api/v1/auth/redeem-bind-token",
            json={"bind_token": token, "device_id": "child_dev_nodb"},
        )

        # 替换 get_db 为抛异常版本
        async def _raise_if_called():
            raise RuntimeError("get_db called but status should not need DB")

        app = api_client._transport.app
        app.dependency_overrides[get_db] = _raise_if_called

        try:
            resp = await api_client.get(f"/api/v1/bind-tokens/{token}/status")
            assert resp.status_code == 200
        finally:
            app.dependency_overrides.pop(get_db, None)

    # ---- A6 · verify_password 非法 hash 异常上抛 ----
    # （在 test_password.py 中实现）

    # ---- A7 · login rate-limit 窗口到期后计数重置 ----
    # （在 test_login_api.py 中实现）

    @pytest.mark.asyncio
    async def test_child_new_device_redeem_revokes_old(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """child 新设备 redeem → 老 token 被吊销。"""
        user, pw = parent_with_password
        device_id = "dev_redeem_C"

        login_resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["token"]

        child_resp = await api_client.post(
            "/api/v1/children",
            json={},
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert child_resp.status_code == 201
        child_id = child_resp.json()["id"]

        # 第一次 redeem
        bind_resp1 = await api_client.post(
            f"/api/v1/children/{child_id}/bind-token",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        token_a = (await api_client.post(
            "/api/v1/auth/redeem-bind-token",
            json={"bind_token": bind_resp1.json()["bind_token"], "device_id": "dev_A"},
        )).json()["token"]

        # parent 生成新 bind_token
        bind_resp2 = await api_client.post(
            f"/api/v1/children/{child_id}/bind-token",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )

        # 新设备 redeem
        await api_client.post(
            "/api/v1/auth/redeem-bind-token",
            json={"bind_token": bind_resp2.json()["bind_token"], "device_id": "dev_B"},
        )

        # token_A + dev_A 再请求 → 401
        old_resp = await api_client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {token_a}", "X-Device-Id": "dev_A"},
        )
        assert old_resp.status_code == 401


# ---- revoke-tokens ----

class TestRevokeChildTokens:
    @pytest.mark.asyncio
    async def test_revoke_child_tokens_happy_path(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """parent 调 revoke-tokens → child 所有 token revoked_at 非空 + Redis 清空。"""
        user, pw = parent_with_password
        device_id = "dev_revoke_A"

        login_resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["token"]

        # 创建 child 并给 child 发行 token
        child_resp = await api_client.post(
            "/api/v1/children",
            json={},
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert child_resp.status_code == 201
        child_id = child_resp.json()["id"]

        bind_resp = await api_client.post(
            f"/api/v1/children/{child_id}/bind-token",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        child_token_resp = await api_client.post(
            "/api/v1/auth/redeem-bind-token",
            json={"bind_token": bind_resp.json()["bind_token"], "device_id": "child_dev_revoke"},
        )
        child_token = child_token_resp.json()["token"]

        # parent revoke
        revoke_resp = await api_client.post(
            f"/api/v1/children/{child_id}/revoke-tokens",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert revoke_resp.status_code == 204

        # child 老 token → 401
        me_resp = await api_client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_dev_revoke"},
        )
        assert me_resp.status_code == 401

    @pytest.mark.asyncio
    async def test_revoke_child_tokens_wrong_family_404(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """parent A 调用 family B 的 child revoke-tokens → 404。"""
        user, pw = parent_with_password
        device_id = "dev_revoke_B"

        login_resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["token"]

        # 造一个不同 family 的 child
        other_fam = Family()
        db_session.add(other_fam)
        await db_session.flush()
        other_child = User(family_id=other_fam.id, role=UserRole.child, is_active=True)
        db_session.add(other_child)
        await db_session.commit()

        resp = await api_client.post(
            f"/api/v1/children/{other_child.id}/revoke-tokens",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_revoke_child_tokens_requires_parent(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """child token 调 revoke-tokens → 403。"""
        user, pw = parent_with_password
        device_id = "dev_revoke_C"

        login_resp = await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["token"]

        # 创建 child
        child_resp = await api_client.post(
            "/api/v1/children",
            json={},
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )
        assert child_resp.status_code == 201
        child_id = child_resp.json()["id"]

        # 给 child 发行 token（child 自己持有）
        child_token = (await api_client.post(
            "/api/v1/auth/redeem-bind-token",
            json={
                "bind_token": (await api_client.post(
                    f"/api/v1/children/{child_id}/bind-token",
                    headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
                )).json()["bind_token"],
                "device_id": "child_dev_revoke",
            },
        )).json()["token"]

        # child 自己调 revoke-tokens → 403
        resp = await api_client.post(
            f"/api/v1/children/{child_id}/revoke-tokens",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_dev_revoke"},
        )
        assert resp.status_code == 403


# ---- bind token status ----

class TestBindTokenStatus:
    @pytest.mark.asyncio
    async def test_bind_status_pending(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """bind_token 生成后未扫 → status=pending。"""
        user, pw = parent_with_password
        device_id = "dev_status_A"

        token = (await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )).json()["token"]

        child_id = (await api_client.post(
            "/api/v1/children",
            json={},
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )).json()["id"]

        bind_token = (await api_client.post(
            f"/api/v1/children/{child_id}/bind-token",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )).json()["bind_token"]

        status_resp = await api_client.get(f"/api/v1/bind-tokens/{bind_token}/status")
        assert status_resp.status_code == 200
        data = status_resp.json()
        assert data["status"] == "pending"
        assert data["child_user_id"] is None
        assert data["bound_at"] is None

    @pytest.mark.asyncio
    async def test_bind_status_bound(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """子端 redeem 后 → status=bound + child_user_id + bound_at。"""
        user, pw = parent_with_password
        device_id = "dev_status_B"

        token = (await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )).json()["token"]

        child_id = (await api_client.post(
            "/api/v1/children",
            json={},
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )).json()["id"]

        bind_token = (await api_client.post(
            f"/api/v1/children/{child_id}/bind-token",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )).json()["bind_token"]

        # 子端 redeem
        await api_client.post(
            "/api/v1/auth/redeem-bind-token",
            json={"bind_token": bind_token, "device_id": "child_dev_status"},
        )

        # 父端查状态
        status_resp = await api_client.get(f"/api/v1/bind-tokens/{bind_token}/status")
        assert status_resp.status_code == 200
        data = status_resp.json()
        assert data["status"] == "bound"
        assert data["child_user_id"] == child_id
        assert data["bound_at"] is not None

    @pytest.mark.asyncio
    async def test_bind_status_no_auth_required(
        self, api_client, db_session: AsyncSession, redis_client, parent_with_password: tuple[User, str],
    ) -> None:
        """GET /bind-tokens/{tok}/status 不需要 Authorization 头。"""
        user, pw = parent_with_password
        device_id = "dev_status_C"

        token = (await api_client.post(
            "/api/v1/auth/login",
            json={"phone": user.phone, "password": pw, "device_id": device_id},
        )).json()["token"]

        child_id = (await api_client.post(
            "/api/v1/children",
            json={},
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )).json()["id"]

        bind_token = (await api_client.post(
            f"/api/v1/children/{child_id}/bind-token",
            headers={"Authorization": f"Bearer {token}", "X-Device-Id": device_id},
        )).json()["bind_token"]

        # 不传 Authorization 也能查
        status_resp = await api_client.get(f"/api/v1/bind-tokens/{bind_token}/status")
        assert status_resp.status_code == 200
