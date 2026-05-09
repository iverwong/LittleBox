"""CLI 业务函数测试（M6-patch 重写）：import 业务函数 + conftest fixture，零 subprocess。

测试隔离铁律（M6-patch）：
- 不调 subprocess / Popen
- 不自建 create_async_engine
- 不拼 os.environ LB_DATABASE_URL
- 所有 DB/Redis 经 conftest fixture 注入
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest

from app.scripts.create_parent import _create_parent
from app.scripts.create_parent import _main as create_parent_main
from app.scripts.reset_parent_password import _reset_password


class TestCreateParent:
    @pytest.mark.asyncio
    async def test_creates_parent_and_family(self, db_session, redis_client):
        """Given: 干净的测试库 + fakeredis
        When: 调用 _create_parent
        Then: ParentInfo 返回; users/families/family_members 各写入 1 行; phone 为 4 字母
        """
        info = await _create_parent(db_session, redis_client, note="unit-test")

        # ParentInfo 字段类型
        assert len(info.phone) == 4
        assert len(info.plain_password) == 8
        assert isinstance(info.user_id, uuid.UUID)
        assert isinstance(info.family_id, uuid.UUID)

        # DB 写入断言
        from app.models.accounts import Family, FamilyMember, User
        from app.models.enums import UserRole

        user = await db_session.get(User, info.user_id)
        assert user is not None
        assert user.phone == info.phone
        assert user.admin_note == "unit-test"
        assert user.role == UserRole.parent
        assert user.is_active is True

        family = await db_session.get(Family, info.family_id)
        assert family is not None

        from sqlalchemy import select

        stmt = select(FamilyMember).where(
            FamilyMember.family_id == info.family_id,
            FamilyMember.user_id == info.user_id,
        )
        fm = (await db_session.execute(stmt)).scalar_one_or_none()
        assert fm is not None
        assert fm.role == UserRole.parent

    @pytest.mark.asyncio
    async def test_admin_note_appears_in_db(self, db_session, redis_client):
        """Given: 不同的 note 值
        When: 分别创建两个 parent
        Then: 每个 parent 的 admin_note 正确对应
        """
        info_a = await _create_parent(db_session, redis_client, note="note-aaa")
        info_b = await _create_parent(db_session, redis_client, note="note-bbb")

        from app.models.accounts import User

        user_a = await db_session.get(User, info_a.user_id)
        user_b = await db_session.get(User, info_b.user_id)
        assert user_a.admin_note == "note-aaa"
        assert user_b.admin_note == "note-bbb"


class TestResetPassword:
    @pytest.mark.asyncio
    async def test_resets_password(self, db_session, redis_client):
        """Given: 已创建的 parent 账号
        When: 调用 _reset_password
        Then: 返回 ResetResult; 新密码 != 原密码; DB 密码 hash 已变更
        """
        info = await _create_parent(db_session, redis_client, note="reset-test")

        from app.models.accounts import User

        old_user = await db_session.get(User, info.user_id)
        old_hash = old_user.password_hash

        result = await _reset_password(db_session, redis_client, phone=info.phone)

        assert result.phone == info.phone
        assert result.plain_password != info.plain_password

        new_user = await db_session.get(User, info.user_id)
        assert new_user.password_hash != old_hash

        # 旧密码不应再通过验证
        from app.auth.password import verify_password

        assert not verify_password(new_user.password_hash, info.plain_password)
        assert verify_password(new_user.password_hash, result.plain_password)

    @pytest.mark.asyncio
    async def test_unknown_phone_raises(self, db_session, redis_client):
        """Given: 不存在的 phone
        When: 调用 _reset_password
        Then: 抛出 ValueError
        """
        with pytest.raises(ValueError, match="no active parent found with phone"):
            await _reset_password(db_session, redis_client, phone="zzzz")


class TestCliEntrypoint:
    @pytest.mark.asyncio
    async def test_create_parent_main_output(self, monkeypatch, capsys, db_session, redis_client):
        """Given: argv 含 --note, cli_runtime 被 monkeypatch 成产出测试 fixture
        When: 调用 _main()
        Then: stdout 含 phone/password/user_id/note
        """
        monkeypatch.setattr("sys.argv", ["create_parent", "--note", "smoke"])

        @asynccontextmanager
        async def _fake_runtime():
            yield (db_session, redis_client)

        monkeypatch.setattr("app.scripts.create_parent.cli_runtime", _fake_runtime)

        await create_parent_main()
        out = capsys.readouterr().out
        assert "✅ parent created" in out
        assert "phone:" in out
        assert "password:" in out
        assert "user_id:" in out
        assert "smoke" in out

    @pytest.mark.asyncio
    async def test_reset_password_main_output(self, monkeypatch, capsys, db_session, redis_client):
        """Given: argv 含 --phone, cli_runtime monkeypatch
        When: 先创建 parent 再调 _main()
        Then: stdout 含 phone/password/user_id
        """
        info = await _create_parent(db_session, redis_client, note="cli-reset")

        monkeypatch.setattr("sys.argv", ["reset_password", "--phone", info.phone])

        @asynccontextmanager
        async def _fake_runtime():
            yield (db_session, redis_client)

        monkeypatch.setattr("app.scripts.reset_parent_password.cli_runtime", _fake_runtime)

        from app.scripts.reset_parent_password import _main as reset_main

        await reset_main()
        out = capsys.readouterr().out
        assert "✅ password reset" in out
        assert info.phone in out
        assert "password:" in out
