"""M4.8 B6 TDD：hard_delete_child 服务层 + Redis 缓存防退化测试。"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.redis_ops import RedisOp, stage_redis_op
from app.auth.tokens import REDIS_KEY_PREFIX
from app.models.accounts import (
    AuthToken,
    ChildProfile,
    DeviceToken,
    Family,
    FamilyMember,
    User,
)
from app.models.chat import Session


class TestHardDeleteChildService:
    """服务层：SELECT COUNT 覆盖 / CASCADE 触发 / 审计记录。"""

    @pytest.mark.asyncio
    async def test_deleted_tables_snapshot(
        self,
        db_session: AsyncSession,
    ) -> None:
        """deleted_tables 包含所有 10 张被 CASCADE 清理的表（无业务过滤）。"""
        from app.auth.password import generate_password, hash_password
        from app.models.enums import DailyStatus
        from app.models.parent import DailyReport, Notification
        from datetime import date

        fam = Family()
        db_session.add(fam)
        await db_session.flush()

        parent = User(
            family_id=fam.id, role="parent",
            phone=generate_password(), password_hash=hash_password(generate_password()),
            is_active=True,
        )
        db_session.add(parent)
        await db_session.flush()

        child = User(family_id=fam.id, role="child", is_active=True)
        db_session.add(child)
        await db_session.flush()

        db_session.add(FamilyMember(family_id=fam.id, user_id=child.id, role="child"))

        db_session.add(ChildProfile(
            child_user_id=child.id, created_by=parent.id,
            birth_date=date(2015, 1, 1), gender="unknown", nickname="test",
        ))
        db_session.add(Session(child_user_id=child.id))
        db_session.add(DeviceToken(user_id=child.id, platform="ios", token="tok123"))
        db_session.add(DailyReport(
            child_user_id=child.id, report_date=date.today(),
            overall_status=DailyStatus.stable, content="test",
        ))
        db_session.add(Notification(parent_user_id=parent.id, child_user_id=child.id, type="crisis"))
        await db_session.commit()

        from app.services.child_deletion import hard_delete_child
        deleted_tables = await hard_delete_child(
            db_session, child_user_id=child.id, requested_by=parent.id,
        )

        assert "child_profiles" in deleted_tables
        assert "sessions" in deleted_tables
        assert "messages" in deleted_tables
        assert "audit_records" in deleted_tables
        assert "rolling_summaries" in deleted_tables
        assert "daily_reports" in deleted_tables
        assert "notifications" in deleted_tables
        assert "auth_tokens" in deleted_tables
        assert "device_tokens" in deleted_tables
        assert "family_members" in deleted_tables

        for table, count in deleted_tables.items():
            assert count >= 0, f"{table} count should be >= 0, got {count}"

        assert deleted_tables["child_profiles"] == 1
        assert deleted_tables["sessions"] == 1
        assert deleted_tables["notifications"] == 1
        assert deleted_tables["device_tokens"] == 1
        assert deleted_tables["family_members"] == 1

    @pytest.mark.asyncio
    async def test_no_active_filter_on_auth_tokens(
        self,
        db_session: AsyncSession,
    ) -> None:
        """SELECT COUNT auth_tokens 不带 revoked_at 过滤，与 CASCADE 实际删除行数一致。"""
        from datetime import datetime, timezone
        from app.auth.password import generate_password, hash_password
        from app.auth.redis_ops import discard_pending_redis_ops

        fam = Family()
        db_session.add(fam)
        await db_session.flush()

        parent = User(
            family_id=fam.id, role="parent",
            phone=generate_password(), password_hash=hash_password(generate_password()),
            is_active=True,
        )
        db_session.add(parent)
        await db_session.flush()

        child = User(family_id=fam.id, role="child", is_active=True)
        db_session.add(child)
        await db_session.flush()

        # 两个 token：一个 active，一个已 revoked
        db_session.add(AuthToken(
            user_id=child.id, token_hash="active_hash",
            device_id="d1", expires_at=None,
        ))
        db_session.add(AuthToken(
            user_id=child.id, token_hash="revoked_hash",
            device_id="d2", expires_at=None,
            revoked_at=datetime.now(timezone.utc),
        ))
        await db_session.commit()

        from app.services.child_deletion import hard_delete_child
        deleted_tables = await hard_delete_child(
            db_session, child_user_id=child.id, requested_by=parent.id,
        )
        await db_session.commit()
        # 手动 discard：conftest 的 savepoint teardown 会 rollback，但 service 内部
        # stage_redis_op 的 delete ops 也被 attach 到 session，需要清掉防止护栏报错
        discard_pending_redis_ops(db_session)

        # count = 2，不做 revoked_at 过滤
        assert deleted_tables["auth_tokens"] == 2

    @pytest.mark.asyncio
    async def test_audit_record_fields(
        self,
        db_session: AsyncSession,
    ) -> None:
        """审计记录：requested_by / child_id_snapshot / reason='parent_request' / deleted_tables。"""
        from app.auth.password import generate_password, hash_password
        from app.models.parent import DataDeletionRequest

        fam = Family()
        db_session.add(fam)
        await db_session.flush()

        parent = User(
            family_id=fam.id, role="parent",
            phone=generate_password(), password_hash=hash_password(generate_password()),
            is_active=True,
        )
        db_session.add(parent)
        await db_session.flush()

        child = User(family_id=fam.id, role="child", is_active=True)
        db_session.add(child)
        await db_session.flush()

        db_session.add(FamilyMember(family_id=fam.id, user_id=child.id, role="child"))
        await db_session.commit()

        from app.services.child_deletion import hard_delete_child
        await hard_delete_child(db_session, child_user_id=child.id, requested_by=parent.id)
        await db_session.commit()

        rows = await db_session.execute(
            select(DataDeletionRequest).where(
                DataDeletionRequest.child_id_snapshot == child.id
            )
        )
        row = rows.first()
        assert row is not None
        ddr = row._mapping[DataDeletionRequest]
        assert ddr.requested_by == parent.id
        assert ddr.child_id_snapshot == child.id
        assert ddr.reason == "parent_request"
        assert isinstance(ddr.deleted_tables, dict)


class TestRedisZombieCache:
    """防退化：裸 commit 留僵尸 Redis 缓存 vs commit_with_redis 正确清理。

    conftest.py 的 db_session teardown 护栏会检测 pending redis ops，
    故意绕开 commit_with_redis 的测试需先 discard_pending_redis_ops。
    """

    @pytest.mark.asyncio
    async def test_redis_cleaned_via_commit_with_redis(
        self,
        db_session: AsyncSession,
        redis_client: FakeRedis,
    ) -> None:
        """正例：commit_with_redis 成功清理 Redis 缓存。"""
        from app.auth.password import generate_password, hash_password
        from app.auth.redis_ops import commit_with_redis
        from app.models.parent import DataDeletionRequest

        fam = Family()
        db_session.add(fam)
        await db_session.flush()

        parent = User(
            family_id=fam.id, role="parent",
            phone=generate_password(), password_hash=hash_password(generate_password()),
            is_active=True,
        )
        db_session.add(parent)
        await db_session.flush()

        child = User(family_id=fam.id, role="child", is_active=True)
        db_session.add(child)
        await db_session.flush()

        db_session.add(FamilyMember(family_id=fam.id, user_id=child.id, role="child"))
        await db_session.flush()

        # 写入 AuthToken + 直接写 Redis（不经 stage）
        th = "verify_token_hash"
        db_session.add(AuthToken(
            user_id=child.id, token_hash=th,
            device_id="d_verify", expires_at=None,
        ))
        await db_session.commit()

        redis_key = f"{REDIS_KEY_PREFIX}{th}"
        await redis_client.setex(redis_key, 600, '{"user_id": "' + str(child.id) + '"}')

        # 验证 Redis 有缓存
        assert await redis_client.get(redis_key) is not None

        # 执行 hard_delete_child + commit_with_redis
        from app.services.child_deletion import hard_delete_child
        await hard_delete_child(db_session, child_user_id=child.id, requested_by=parent.id)
        await commit_with_redis(db_session, redis_client)

        # 验证 Redis 缓存已清理
        assert await redis_client.get(redis_key) is None

        # 验证审计记录存在
        rows = await db_session.execute(
            select(DataDeletionRequest).where(
                DataDeletionRequest.child_id_snapshot == child.id
            )
        )
        assert rows.first() is not None

    @pytest.mark.asyncio
    async def test_bare_commit_leaves_zombie_redis(
        self,
        db_session: AsyncSession,
        redis_client: FakeRedis,
    ) -> None:
        """反例：hard_delete_child 后裸 commit → Redis 缓存残留（防退化）。"""
        from app.auth.password import generate_password, hash_password
        from app.auth.redis_ops import discard_pending_redis_ops

        fam = Family()
        db_session.add(fam)
        await db_session.flush()

        parent = User(
            family_id=fam.id, role="parent",
            phone=generate_password(), password_hash=hash_password(generate_password()),
            is_active=True,
        )
        db_session.add(parent)
        await db_session.flush()

        child = User(family_id=fam.id, role="child", is_active=True)
        db_session.add(child)
        await db_session.flush()

        db_session.add(FamilyMember(family_id=fam.id, user_id=child.id, role="child"))
        await db_session.flush()

        # 直接写 Redis（不经 stage_redis_op）
        th = "zombie_token"
        db_session.add(AuthToken(
            user_id=child.id, token_hash=th,
            device_id="dz", expires_at=None,
        ))
        await db_session.flush()
        await db_session.commit()

        redis_key = f"{REDIS_KEY_PREFIX}{th}"
        await redis_client.setex(redis_key, 600, '{"user_id": "' + str(child.id) + '"}')

        assert await redis_client.get(redis_key) is not None

        from app.services.child_deletion import hard_delete_child
        await hard_delete_child(db_session, child_user_id=child.id, requested_by=parent.id)
        # 裸 commit（绕开 commit_with_redis）
        await db_session.commit()

        # 手动 discard pending ops（防止 conftest 护栏干扰）
        discard_pending_redis_ops(db_session)

        # Redis 缓存仍然残留
        assert await redis_client.get(redis_key) is not None, (
            "BUG: Redis cache should still exist after bare commit"
        )
