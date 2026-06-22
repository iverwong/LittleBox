"""M10/M11 TDD:update_child_profile service 行为。

覆盖(M10 → M11 后改为全量提交):
- PUT 全量替换(nickname / birth_date / gender / concerns / sensitivity /
  custom_redlines 全部按提交值落库)
- 必输字段校验(field_validator [3, 21] + 空串归一)
- 跨 family 访问 → 404
- snapshot builder 正确填所有字段

测试函数级 docstring 用 Given / When / Then。"""

from __future__ import annotations

from datetime import date

import pytest
from app.core.enums import Gender, UserRole
from app.domain.accounts.models import ChildProfile, Family, FamilyMember, User
from app.domain.accounts.schemas import (
    CurrentAccount,
    PutChildProfileRequest,
    SensitivityConfig,
)
from app.domain.accounts.service import (
    age_to_birth_date,
    build_child_profile_snapshot,
    load_child_profile_in_family,
    update_child_profile,
)
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
async def parent_in_family(db_session: AsyncSession):
    """种一个 active parent + family,返回 (family, parent_user, account_ctx)。"""
    fam = Family()
    db_session.add(fam)
    await db_session.flush()

    parent = User(
        family_id=fam.id,
        role=UserRole.parent,
        phone="p1",
        is_active=True,
    )
    db_session.add(parent)
    await db_session.flush()

    db_session.add(FamilyMember(family_id=fam.id, user_id=parent.id, role=UserRole.parent))
    await db_session.commit()

    ctx = CurrentAccount(id=parent.id, role=parent.role, family_id=fam.id, expires_at=None)
    return fam, parent, ctx


@pytest.fixture
async def child_with_profile(db_session: AsyncSession, parent_in_family):
    """在 parent_in_family 的 family 内种一个 child + ChildProfile。"""
    fam, _, _ = parent_in_family

    child = User(family_id=fam.id, role=UserRole.child, is_active=True)
    db_session.add(child)
    await db_session.flush()

    profile = ChildProfile(
        child_user_id=child.id,
        created_by=parent_in_family[1].id,
        birth_date=age_to_birth_date(10),
        gender=Gender.male,
        nickname="orig_nick",
        concerns="orig concerns",
        sensitivity={
            "emotional": 5,
            "social": 5,
            "values": 5,
            "boundaries": 5,
            "academic": 5,
            "lifestyle": 5,
        },
        custom_redlines="orig redline",
    )
    db_session.add(profile)
    await db_session.commit()

    return child, profile


@pytest.fixture
async def other_family(db_session: AsyncSession):
    """种一个独立的 family + parent(用于跨 family 访问测试)。"""
    other_fam = Family()
    db_session.add(other_fam)
    await db_session.flush()

    other_parent = User(
        family_id=other_fam.id,
        role=UserRole.parent,
        phone="p2",
        is_active=True,
    )
    db_session.add(other_parent)
    await db_session.flush()

    db_session.add(
        FamilyMember(family_id=other_fam.id, user_id=other_parent.id, role=UserRole.parent)
    )
    await db_session.commit()
    return other_fam, other_parent


class TestPutChildProfile:
    """PUT 语义:payload 携带全部字段,服务层显式逐字段全量赋值。"""

    @pytest.mark.asyncio
    async def test_full_update_persists_all_fields(
        self,
        db_session: AsyncSession,
        parent_in_family,
        child_with_profile,
        redis_client,
    ) -> None:
        """Given 已存在的 child profile
        When PUT 提交 6 字段全部新值
        Then DB 6 字段按提交值落库,无残留。
        """
        _, _, ctx = parent_in_family
        child, _ = child_with_profile

        payload = PutChildProfileRequest(
            nickname="new_nick",
            birth_date=date(2014, 6, 15),
            gender=Gender.female,
            sensitivity=SensitivityConfig(
                emotional=9, social=2, values=7, boundaries=8, academic=1, lifestyle=4
            ),
            concerns="new concerns",
            custom_redlines="new redline",
        )
        await update_child_profile(
            db_session,
            redis_client,
            parent=ctx,
            child_user_id=child.id,
            payload=payload,
        )

        refreshed = await load_child_profile_in_family(
            db_session, child_user_id=child.id, family_id=ctx.family_id
        )
        assert refreshed is not None
        assert refreshed.nickname == "new_nick"
        assert refreshed.birth_date == date(2014, 6, 15)
        assert refreshed.gender == Gender.female
        assert refreshed.sensitivity == {
            "emotional": 9,
            "social": 2,
            "values": 7,
            "boundaries": 8,
            "academic": 1,
            "lifestyle": 4,
        }
        assert refreshed.concerns == "new concerns"
        assert refreshed.custom_redlines == "new redline"

    @pytest.mark.asyncio
    async def test_blank_concerns_normalized_to_none(
        self,
        db_session: AsyncSession,
        parent_in_family,
        child_with_profile,
        redis_client,
    ) -> None:
        """Given PUT concerns="   "(纯空格)
        When update_child_profile
        Then DB 落库 concerns=None(field_validator 空串归一)。
        """
        _, _, ctx = parent_in_family
        child, _ = child_with_profile

        payload = PutChildProfileRequest(
            nickname="new_nick",
            birth_date=date(2015, 6, 1),
            gender=Gender.male,
            sensitivity=SensitivityConfig(),
            concerns="   ",
            custom_redlines=None,
        )
        await update_child_profile(
            db_session,
            redis_client,
            parent=ctx,
            child_user_id=child.id,
            payload=payload,
        )

        refreshed = await load_child_profile_in_family(
            db_session, child_user_id=child.id, family_id=ctx.family_id
        )
        assert refreshed is not None
        assert refreshed.concerns is None
        assert refreshed.custom_redlines is None

    def test_birth_date_too_young_rejected(self) -> None:
        """Given PutChildProfileRequest(birth_date=今天,~0 岁)
        When 构造 payload
        Then Pydantic ValidationError(age 范围 [3, 21])。
        """
        with pytest.raises(ValidationError) as exc_info:
            PutChildProfileRequest(
                nickname="x",
                birth_date=date.today(),
                gender=Gender.male,
                sensitivity=SensitivityConfig(),
            )
        assert "birth_date" in str(exc_info.value)

    def test_birth_date_too_old_rejected(self) -> None:
        """Given PutChildProfileRequest(birth_date=1980,~46 岁)
        When 构造 payload
        Then Pydantic ValidationError。
        """
        with pytest.raises(ValidationError) as exc_info:
            PutChildProfileRequest(
                nickname="x",
                birth_date=date(1980, 1, 1),
                gender=Gender.male,
                sensitivity=SensitivityConfig(),
            )
        assert "birth_date" in str(exc_info.value)

    def test_nickname_too_long_rejected(self) -> None:
        """Given PutChildProfileRequest(nickname 长度 13)
        When 构造 payload
        Then Pydantic ValidationError(max_length=12)。
        """
        with pytest.raises(ValidationError) as exc_info:
            PutChildProfileRequest(
                nickname="x" * 13,
                birth_date=date(2015, 6, 1),
                gender=Gender.male,
                sensitivity=SensitivityConfig(),
            )
        assert "nickname" in str(exc_info.value)


class TestUpdateChildProfileCrossFamilyForbidden:
    """跨 family 访问 → 404,不暴露存在性。"""

    @pytest.mark.asyncio
    async def test_other_family_parent_404(
        self,
        db_session: AsyncSession,
        parent_in_family,
        child_with_profile,
        other_family,
        redis_client,
    ) -> None:
        """Given 别的 family 的父账号 + 本 family 的 child
        When update_child_profile
        Then 抛 HTTPException 404。
        """
        _, _, _ = parent_in_family
        child, _ = child_with_profile
        _, other_parent = other_family

        other_ctx = CurrentAccount(
            id=other_parent.id,
            role=other_parent.role,
            family_id=other_parent.family_id,
            expires_at=None,
        )

        payload = PutChildProfileRequest(
            nickname="hack",
            birth_date=date(2015, 6, 1),
            gender=Gender.male,
            sensitivity=SensitivityConfig(),
        )
        with pytest.raises(HTTPException) as exc_info:
            await update_child_profile(
                db_session,
                redis_client,
                parent=other_ctx,
                child_user_id=child.id,
                payload=payload,
            )
        assert exc_info.value.status_code == 404


class TestBuildChildProfileSnapshot:
    """snapshot builder 正确填所有字段。"""

    @pytest.mark.asyncio
    async def test_snapshot_includes_concerns(
        self,
        db_session: AsyncSession,
        parent_in_family,
        child_with_profile,
    ) -> None:
        """Given 已加载的 ChildProfile(含 concerns)
        When build_child_profile_snapshot
        Then snapshot.concerns 等于 ORM 的值。
        """
        _, _, ctx = parent_in_family
        _, profile = child_with_profile

        snap = build_child_profile_snapshot(profile)
        assert snap.concerns == "orig concerns"
        assert snap.sensitivity == profile.sensitivity
        assert snap.custom_redlines == "orig redline"
        assert snap.nickname == "orig_nick"
        assert snap.gender == "male"
