"""M4.8 B1 TDD：ChildProfile.nickname 约束验证。"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounts import ChildProfile, Family, User
from app.models.enums import UserRole


@pytest_asyncio.fixture
async def _family_and_parent(db_session: AsyncSession) -> tuple[Family, User]:
    """最小 family + parent，用于构造 ChildProfile。"""
    fam = Family()
    db_session.add(fam)
    await db_session.flush()

    parent = User(
        family_id=fam.id,
        role=UserRole.parent,
        phone="aaaa",
        is_active=True,
    )
    db_session.add(parent)
    await db_session.flush()
    return fam, parent


class TestNicknameRequired:
    @pytest.mark.asyncio
    async def test_nickname_required_on_create(
        self,
        db_session: AsyncSession,
        _family_and_parent: tuple[Family, User],
    ) -> None:
        """实例化 ChildProfile 不传 nickname → sqlalchemy.exc.IntegrityError（NOT NULL）"""
        fam, parent = _family_and_parent

        child = User(
            family_id=fam.id,
            role=UserRole.child,
            is_active=True,
        )
        db_session.add(child)
        await db_session.flush()

        profile = ChildProfile(
            child_user_id=child.id,
            created_by=parent.id,
            # nickname 省略 → NOT NULL 违例
        )
        db_session.add(profile)
        with pytest.raises(sa_exc.IntegrityError):
            await db_session.flush()
        await db_session.rollback()


class TestNicknameMaxLength:
    @pytest.mark.asyncio
    async def test_nickname_32_chars_ok(
        self,
        db_session: AsyncSession,
        _family_and_parent: tuple[Family, User],
    ) -> None:
        """nickname 32 字符 → 成功（边界值）"""
        fam, parent = _family_and_parent

        child = User(family_id=fam.id, role=UserRole.child, is_active=True)
        db_session.add(child)
        await db_session.flush()

        profile = ChildProfile(
            child_user_id=child.id,
            created_by=parent.id,
            nickname="a" * 32,
        )
        db_session.add(profile)
        await db_session.flush()  # 不抛即通过
        await db_session.rollback()

    @pytest.mark.asyncio
    async def test_nickname_max_length_32(
        self,
        db_session: AsyncSession,
        _family_and_parent: tuple[Family, User],
    ) -> None:
        """nickname 33 字符 → PostgreSQL 抛 StringDataRightTruncation（VARCHAR(32) 约束）"""
        fam, parent = _family_and_parent

        child = User(family_id=fam.id, role=UserRole.child, is_active=True)
        db_session.add(child)
        await db_session.flush()

        profile = ChildProfile(
            child_user_id=child.id,
            created_by=parent.id,
            nickname="a" * 33,  # 超出 VARCHAR(32)
        )
        db_session.add(profile)
        # PostgreSQL VARCHAR(32) 强制截断检查
        with pytest.raises(Exception) as exc_info:
            await db_session.flush()
        # 验证是 DB 层面异常（非 SQLAlchemy 包装）
        assert "StringDataRightTruncation" in str(type(exc_info.value).__name__) or \
               "value too long" in str(exc_info.value).lower()
        await db_session.rollback()
