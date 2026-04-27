"""M4.8 B6 TDD：DELETE /api/v1/children/{id} 端点测试。"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import func, select

from app.models.accounts import (
    AuthToken,
    ChildProfile,
    Family,
    FamilyMember,
    User,
)
from app.models.chat import Session
from app.models.enums import UserRole
from app.models.parent import DataDeletionRequest, DailyReport, Notification


async def _login(api_client, user: User, pw: str, device_id: str = "test_device") -> str:
    login_resp = await api_client.post(
        "/api/v1/auth/login",
        json={"phone": user.phone, "password": pw, "device_id": device_id},
    )
    return login_resp.json()["token"]


async def _create_child_via_api(api_client, parent_token: str, nickname: str = "小明", age: int = 10) -> dict:
    resp = await api_client.post(
        "/api/v1/children",
        json={"nickname": nickname, "age": age, "gender": "unknown"},
        headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": "test_device"},
    )
    assert resp.status_code == 201
    return resp.json()


class TestDeleteChildSuccess:
    """正例：CASCADE 全链清空 + 豁免项保留。"""

    @pytest.mark.asyncio
    async def test_cascade_deletes_all_tables(
        self,
        api_client,
        db_session,
        seeded_parent,
    ) -> None:
        """DELETE child → 各关联表行数归零；families / parent / parent_family_members 保留。"""
        from app.models.enums import DailyStatus

        parent, pw = seeded_parent
        parent_token = await _login(api_client, parent, pw)

        child_json = await _create_child_via_api(api_client, parent_token)
        child_id = uuid.UUID(child_json["id"])

        # 绑定 child token
        bind_resp = await api_client.post(
            "/api/v1/bind-tokens",
            json={"child_user_id": str(child_id)},
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": "test_device"},
        )
        bind_token = bind_resp.json()["bind_token"]
        redeem_resp = await api_client.post(
            f"/api/v1/bind-tokens/{bind_token}/redeem",
            json={"device_id": "child_device"},
        )
        assert redeem_resp.status_code == 200

        # 写一条日终报告
        db_session.add(DailyReport(
            child_user_id=child_id,
            report_date=date.today(),
            overall_status=DailyStatus.stable,
            content="today summary",
        ))

        # 写一条系统级通知（child_user_id IS NULL）
        db_session.add(Notification(
            parent_user_id=parent.id,
            child_user_id=None,
            type="daily_summary",
        ))
        await db_session.commit()

        # DELETE child
        resp = await api_client.delete(
            f"/api/v1/children/{child_id}",
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 204, resp.text

        # 验证 child_profiles 已清
        count = await db_session.scalar(
            select(func.count()).select_from(ChildProfile).where(ChildProfile.child_user_id == child_id)
        )
        assert count == 0

        # 验证 sessions 已清
        count = await db_session.scalar(
            select(func.count()).select_from(Session).where(Session.child_user_id == child_id)
        )
        assert count == 0

        # 验证 daily_reports 已清
        count = await db_session.scalar(
            select(func.count()).select_from(DailyReport).where(DailyReport.child_user_id == child_id)
        )
        assert count == 0

        # 验证 notifications[child_user_id=?] 已清
        count = await db_session.scalar(
            select(func.count()).select_from(Notification).where(Notification.child_user_id == child_id)
        )
        assert count == 0

        # 验证 family_members 中 child 记录已清
        count = await db_session.scalar(
            select(func.count()).select_from(FamilyMember).where(FamilyMember.user_id == child_id)
        )
        assert count == 0

        # 验证系统通知（child_user_id IS NULL）保留
        count = await db_session.scalar(
            select(func.count()).select_from(Notification).where(Notification.child_user_id.is_(None))
        )
        assert count == 1

        # 验证 parent 未受影响
        count = await db_session.scalar(
            select(func.count()).select_from(User).where(User.id == parent.id)
        )
        assert count == 1

    @pytest.mark.asyncio
    async def test_audit_record_written(
        self,
        api_client,
        db_session,
        seeded_parent,
    ) -> None:
        """DataDeletionRequest 审计行：child_id_snapshot / deleted_tables / reason / created_at。"""
        parent, pw = seeded_parent
        parent_token = await _login(api_client, parent, pw)

        child_json = await _create_child_via_api(api_client, parent_token)
        child_id = uuid.UUID(child_json["id"])

        resp = await api_client.delete(
            f"/api/v1/children/{child_id}",
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 204

        rows = await db_session.execute(
            select(DataDeletionRequest).where(DataDeletionRequest.child_id_snapshot == child_id)
        )
        row = rows.first()
        assert row is not None
        ddr = row._mapping[DataDeletionRequest]
        assert ddr.requested_by == parent.id
        assert ddr.child_id_snapshot == child_id
        assert ddr.reason == "parent_request"
        assert isinstance(ddr.deleted_tables, dict)
        assert "child_profiles" in ddr.deleted_tables
        assert "sessions" in ddr.deleted_tables


class TestDeleteChildAuth:
    """错误码矩阵：401 / 403 / 404。"""

    @pytest.mark.asyncio
    async def test_unauthenticated_401(self, api_client) -> None:
        """未登录 → 401。"""
        fake_id = uuid.uuid4()
        resp = await api_client.delete(f"/api/v1/children/{fake_id}")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_child_token_forbidden(
        self,
        api_client,
        seeded_parent,
    ) -> None:
        """child token → 403。"""
        parent, pw = seeded_parent
        parent_token = await _login(api_client, parent, pw)

        child_json = await _create_child_via_api(api_client, parent_token)
        child_id = uuid.UUID(child_json["id"])

        bind_resp = await api_client.post(
            "/api/v1/bind-tokens",
            json={"child_user_id": str(child_id)},
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": "test_device"},
        )
        bind_token = bind_resp.json()["bind_token"]
        redeem_resp = await api_client.post(
            f"/api/v1/bind-tokens/{bind_token}/redeem",
            json={"device_id": "child_device"},
        )
        child_token = redeem_resp.json()["token"]

        resp = await api_client.delete(
            f"/api/v1/children/{child_id}",
            headers={"Authorization": f"Bearer {child_token}", "X-Device-Id": "child_device"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_nonexistent_id_404(
        self,
        api_client,
        seeded_parent,
    ) -> None:
        """不存在的 id → 404。"""
        parent, pw = seeded_parent
        parent_token = await _login(api_client, parent, pw)
        fake_id = uuid.uuid4()
        resp = await api_client.delete(
            f"/api/v1/children/{fake_id}",
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_parent_id_404(
        self,
        api_client,
        seeded_parent,
    ) -> None:
        """误传 parent id → 404（不暴露 role，不暴露存在性）。"""
        parent, pw = seeded_parent
        parent_token = await _login(api_client, parent, pw)
        resp = await api_client.delete(
            f"/api/v1/children/{parent.id}",
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_cross_family_404(
        self,
        api_client,
        db_session,
        seeded_parent,
    ) -> None:
        """跨家族 child → 404（不暴露存在性）。"""
        from app.auth.password import generate_password, generate_phone

        parent, pw = seeded_parent
        parent_token = await _login(api_client, parent, pw)

        other_fam = Family()
        db_session.add(other_fam)
        await db_session.flush()
        other_child = User(
            family_id=other_fam.id, role=UserRole.child,
            phone=generate_phone(), is_active=True,
        )
        db_session.add(other_child)
        await db_session.flush()
        db_session.add(FamilyMember(
            family_id=other_fam.id, user_id=other_child.id, role=UserRole.child
        ))
        await db_session.commit()

        resp = await api_client.delete(
            f"/api/v1/children/{other_child.id}",
            headers={"Authorization": f"Bearer {parent_token}", "X-Device-Id": "test_device"},
        )
        assert resp.status_code == 404


class TestDeleteChildTransactionRollback:
    """事务异常回滚 → DB / Redis 均不变。"""

    @pytest.mark.asyncio
    async def test_rollback_on_service_error(
        self,
        db_session,
        redis_client,
        seeded_parent,
        monkeypatch,
    ) -> None:
        """hard_delete_child 中途异常 → teardown rollback → DB / Redis 均不变。

        注入点：DELETE User 阶段抛 SQLAlchemyError（模拟 CASCADE 中途连接抖动）。
        验证：child 仍存在 / 审计行未生成 / Redis 缓存未被动删除。
        conftest 的 teardown 会自动 rollback，异常后 session 不支持再次 await，
        所以不显式调用 rollback。
        """
        from sqlalchemy.exc import SQLAlchemyError

        from app.auth.password import generate_password
        from app.auth.redis_ops import discard_pending_redis_ops
        from app.auth.tokens import REDIS_KEY_PREFIX
        from app.services.child_deletion import hard_delete_child

        parent, _pw = seeded_parent

        # Setup: 创建 child + token + 直接写 Redis（不经 stage）
        child = User(
            family_id=parent.family_id, role=UserRole.child,
            phone=generate_password(), is_active=True,
        )
        db_session.add(child)
        await db_session.flush()
        db_session.add(FamilyMember(
            family_id=parent.family_id, user_id=child.id, role=UserRole.child
        ))
        db_session.add(ChildProfile(
            child_user_id=child.id, created_by=parent.id,
            birth_date=date(2015, 1, 1), gender="unknown", nickname="rollback_test",
        ))
        await db_session.flush()

        th = "rollback_token_hash"
        db_session.add(AuthToken(
            user_id=child.id, token_hash=th,
            device_id="d_rollback", expires_at=None,
        ))
        await db_session.flush()
        await db_session.commit()

        # 直接写 Redis（绕过 stage 系统）
        redis_key = f"{REDIS_KEY_PREFIX}{th}"
        await redis_client.setex(redis_key, 600, '{"user_id": "' + str(child.id) + '"}')

        # 前置：Redis 有缓存
        assert await redis_client.get(redis_key) is not None

        # 注入：DELETE User 阶段抛错（monkeypatch 替换 execute）
        delete_called = {"flag": False}

        async def faulty_execute(stmt, *args, **kwargs):
            if "DELETE FROM users" in str(stmt):
                delete_called["flag"] = True
                raise SQLAlchemyError("simulated DB failure during DELETE User")
            return await original_execute(stmt, *args, **kwargs)

        original_execute = db_session.execute
        monkeypatch.setattr(db_session, "execute", faulty_execute)

        try:
            with pytest.raises(SQLAlchemyError):
                await hard_delete_child(
                    db_session,
                    child_user_id=child.id,
                    requested_by=parent.id,
                )
            assert delete_called["flag"]
        finally:
            monkeypatch.setattr(db_session, "execute", original_execute)
            discard_pending_redis_ops(db_session)

        # teardown 会自动 rollback，所以这里不再显式调用 await db_session.rollback()
        # DB 验证：child 仍在、审计行未生成
        from app.models.parent import DataDeletionRequest
        child_count = await db_session.scalar(
            select(func.count()).select_from(User).where(User.id == child.id)
        )
        assert child_count == 1, "child should still exist after rollback"
        ddr_count = await db_session.scalar(
            select(func.count()).select_from(DataDeletionRequest).where(
                DataDeletionRequest.child_id_snapshot == child.id
            )
        )
        assert ddr_count == 0, "DataDeletionRequest should not be written after rollback"

        # Redis 验证：缓存未被清理
        assert await redis_client.get(redis_key) is not None, "Redis cache should survive rollback"
