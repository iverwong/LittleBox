"""M10 TDD:update_child_profile service 行为。

覆盖:
- 部分更新只动传入字段(其它字段保持原值)
- `age` 重算 `birth_date`
- 可空字段 `null` 即清空
- sensitivity 整体替换
- 跨 family 访问 → 404

测试函数级 docstring 用 Given / When / Then。"""

from __future__ import annotations

from datetime import date

import pytest
from app.core.enums import Gender, UserRole
from app.domain.accounts.models import ChildProfile, Family, FamilyMember, User
from app.domain.accounts.schemas import (
    CurrentAccount,
    SensitivityConfig,
    UpdateChildProfileRequest,
)
from app.domain.accounts.service import (
    age_to_birth_date,
    build_child_profile_snapshot,
    load_child_profile_in_family,
    update_child_profile,
)
from fastapi import HTTPException
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

    ctx = CurrentAccount(
        id=parent.id, role=parent.role, family_id=fam.id, expires_at=None
    )
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


class TestUpdateChildProfilePartial:
    """PATCH 语义:仅 `exclude_unset` 的字段参与更新。"""

    @pytest.mark.asyncio
    async def test_only_nickname_changed(
        self,
        db_session: AsyncSession,
        parent_in_family,
        child_with_profile,
        redis_client,
    ) -> None:
        """Given 已存在的 child profile
        When 仅 PATCH nickname
        Then nickname 更新,其它字段(gender / age-derived birth_date /
            concerns / sensitivity / custom_redlines)保持原值。
        """
        _, _, ctx = parent_in_family
        child, profile = child_with_profile

        await update_child_profile(
            db_session,
            redis_client,
            parent=ctx,
            child_user_id=child.id,
            payload=UpdateChildProfileRequest(nickname="new_nick"),
        )

        refreshed = await load_child_profile_in_family(
            db_session, child_user_id=child.id, family_id=ctx.family_id
        )
        assert refreshed is not None
        assert refreshed.nickname == "new_nick"
        assert refreshed.gender == Gender.male
        assert refreshed.birth_date == profile.birth_date
        assert refreshed.concerns == "orig concerns"
        assert refreshed.custom_redlines == "orig redline"
        assert refreshed.sensitivity == profile.sensitivity

    @pytest.mark.asyncio
    async def test_age_recomputes_birth_date(
        self,
        db_session: AsyncSession,
        parent_in_family,
        child_with_profile,
        redis_client,
    ) -> None:
        """Given PATCH age=12
        When update_child_profile
        Then birth_date 重算为 today() - 12 years(其他字段不变)。
        """
        _, _, ctx = parent_in_family
        child, profile = child_with_profile
        original_nickname = profile.nickname

        await update_child_profile(
            db_session,
            redis_client,
            parent=ctx,
            child_user_id=child.id,
            payload=UpdateChildProfileRequest(age=12),
        )

        refreshed = await load_child_profile_in_family(
            db_session, child_user_id=child.id, family_id=ctx.family_id
        )
        assert refreshed is not None
        assert refreshed.birth_date == age_to_birth_date(12)
        assert refreshed.nickname == original_nickname
        assert refreshed.gender == Gender.male


class TestUpdateChildProfileNullableClear:
    """可空字段传 null 即清空。"""

    @pytest.mark.asyncio
    async def test_concerns_null_clears(
        self,
        db_session: AsyncSession,
        parent_in_family,
        child_with_profile,
        redis_client,
    ) -> None:
        """Given PATCH concerns=null
        When update_child_profile
        Then profile.concerns 变为 None(其它字段保留)。
        """
        _, _, ctx = parent_in_family
        child, _ = child_with_profile

        await update_child_profile(
            db_session,
            redis_client,
            parent=ctx,
            child_user_id=child.id,
            payload=UpdateChildProfileRequest(concerns=None),
        )

        refreshed = await load_child_profile_in_family(
            db_session, child_user_id=child.id, family_id=ctx.family_id
        )
        assert refreshed is not None
        assert refreshed.concerns is None
        # 其它字段保留
        assert refreshed.custom_redlines == "orig redline"
        assert refreshed.nickname == "orig_nick"

    @pytest.mark.asyncio
    async def test_sensitivity_null_clears(
        self,
        db_session: AsyncSession,
        parent_in_family,
        child_with_profile,
        redis_client,
    ) -> None:
        """Given PATCH sensitivity=null
        When update_child_profile
        Then profile.sensitivity 变为 None(JSONB 列允许 null)。
        """
        _, _, ctx = parent_in_family
        child, _ = child_with_profile

        await update_child_profile(
            db_session,
            redis_client,
            parent=ctx,
            child_user_id=child.id,
            payload=UpdateChildProfileRequest(sensitivity=None),
        )

        refreshed = await load_child_profile_in_family(
            db_session, child_user_id=child.id, family_id=ctx.family_id
        )
        assert refreshed is not None
        assert refreshed.sensitivity is None


class TestUpdateChildProfileSensitivityReplace:
    """sensitivity 整体替换(不做维度级 merge)。"""

    @pytest.mark.asyncio
    async def test_sensitivity_replaces_whole_dict(
        self,
        db_session: AsyncSession,
        parent_in_family,
        child_with_profile,
        redis_client,
    ) -> None:
        """Given PATCH sensitivity=新 6 维字典
        When update_child_profile
        Then 整个 JSONB 被替换为新字典(原 dict 完全消失)。
        """
        _, _, ctx = parent_in_family
        child, _ = child_with_profile

        new_sensitivity = SensitivityConfig(
            emotional=9, social=2, values=7, boundaries=8, academic=1, lifestyle=4
        )
        await update_child_profile(
            db_session,
            redis_client,
            parent=ctx,
            child_user_id=child.id,
            payload=UpdateChildProfileRequest(sensitivity=new_sensitivity),
        )

        refreshed = await load_child_profile_in_family(
            db_session, child_user_id=child.id, family_id=ctx.family_id
        )
        assert refreshed is not None
        # 整体替换后的 6 维与原 dict 完全无关
        assert refreshed.sensitivity == {
            "emotional": 9,
            "social": 2,
            "values": 7,
            "boundaries": 8,
            "academic": 1,
            "lifestyle": 4,
        }


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

        with pytest.raises(HTTPException) as exc_info:
            await update_child_profile(
                db_session,
                redis_client,
                parent=other_ctx,
                child_user_id=child.id,
                payload=UpdateChildProfileRequest(nickname="hack"),
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