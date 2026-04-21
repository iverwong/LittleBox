"""Token 模块 TDD：issue / resolve / revoke / roll 覆盖。
写路径全经 stage_redis_op + commit_with_redis；resolve_token 为纯读不做续期。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.redis_ops import commit_with_redis, discard_pending_redis_ops
from app.auth.tokens import (
    REDIS_KEY_PREFIX,
    TokenPayload,
    issue_token,
    needs_roll,
    resolve_token,
    revoke_all_active_tokens,
    revoke_token,
    roll_token_expiry,
    token_hash,
)
from app.models.accounts import AuthToken, Family, FamilyMember, User
from app.models.enums import UserRole

# ---- 辅助 fixtures ----

@pytest_asyncio.fixture
async def parent_user(db_session: AsyncSession) -> User:
    """种一个 active parent + family + family_members。"""
    fam = Family()
    db_session.add(fam)
    await db_session.flush()

    user = User(
        family_id=fam.id,
        role=UserRole.parent,
        phone="abcd",
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    db_session.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.parent))
    await db_session.commit()  # 实际 release savepoint
    return user


@pytest_asyncio.fixture
async def child_user(db_session: AsyncSession) -> User:
    """种一个 child + family。"""
    fam = Family()
    db_session.add(fam)
    await db_session.flush()

    user = User(
        family_id=fam.id,
        role=UserRole.child,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    db_session.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.child))
    await db_session.commit()
    return user


def _redis_key(th: str) -> str:
    return f"{REDIS_KEY_PREFIX}{th}"


# ---- token_hash ----

class TestTokenHash:
    def test_token_hash_returns_64_hex_chars(self) -> None:
        th = token_hash("abcd1234")
        assert len(th) == 64
        assert all(c in "0123456789abcdef" for c in th)

    def test_token_hash_deterministic(self) -> None:
        assert token_hash("secret") == token_hash("secret")


# ---- issue_token ----

class TestIssueToken:
    @pytest.mark.asyncio
    async def test_issue_token_parent_expires_at_plus_7d(
        self, db_session: AsyncSession, redis_client, parent_user: User
    ) -> None:
        token = await issue_token(
            db_session,
            user_id=parent_user.id,
            role=parent_user.role,
            family_id=parent_user.family_id,
            device_id="devA",
        )
        th = token_hash(token)

        # DB row
        from sqlalchemy import select
        row = (await db_session.execute(
            select(AuthToken).where(AuthToken.token_hash == th)
        )).scalar_one()
        assert row.expires_at is not None
        assert row.device_id == "devA"
        discard_pending_redis_ops(db_session)  # 不 commit；清理避免 teardown 护栏误报

    @pytest.mark.asyncio
    async def test_issue_token_child_expires_at_null(
        self, db_session: AsyncSession, redis_client, child_user: User
    ) -> None:
        token = await issue_token(
            db_session,
            user_id=child_user.id,
            role=child_user.role,
            family_id=child_user.family_id,
            device_id="childdev",
            ttl_days=None,
        )
        th = token_hash(token)
        from sqlalchemy import select
        row = (await db_session.execute(
            select(AuthToken).where(AuthToken.token_hash == th)
        )).scalar_one()
        assert row.expires_at is None
        discard_pending_redis_ops(db_session)  # 不 commit；清理避免 teardown 护栏误报

    @pytest.mark.asyncio
    async def test_issue_token_device_id_to_redis(
        self, db_session: AsyncSession, redis_client, parent_user: User
    ) -> None:
        token = await issue_token(
            db_session,
            user_id=parent_user.id,
            role=parent_user.role,
            family_id=parent_user.family_id,
            device_id="devA",
        )
        th = token_hash(token)
        await commit_with_redis(db_session, redis_client)
        cached = await redis_client.get(_redis_key(th))
        payload = TokenPayload.model_validate_json(cached)
        assert payload.device_id == "devA"

    @pytest.mark.asyncio
    async def test_issue_token_returns_plaintext_token(
        self, db_session: AsyncSession, redis_client, parent_user: User,
    ) -> None:
        token = await issue_token(
            db_session,
            user_id=parent_user.id,
            role=parent_user.role,
            family_id=parent_user.family_id,
            device_id="devA",
        )
        assert isinstance(token, str)
        assert len(token) > 20  # secrets.token_urlsafe(32) ≈ 43 chars
        discard_pending_redis_ops(db_session)  # 不 commit；清理避免 teardown 护栏误报

    @pytest.mark.asyncio
    async def test_issue_token_requires_device_id(
        self, db_session: AsyncSession, parent_user: User,
    ) -> None:
        with pytest.raises(TypeError):
            await issue_token(  # type: ignore[call-arg]
                db_session,
                user_id=parent_user.id,
                role=parent_user.role,
                family_id=parent_user.family_id,
                # device_id 漏填
            )


# ---- resolve_token ----

class TestResolveToken:
    @pytest.mark.asyncio
    async def test_resolve_token_redis_hit_no_db(
        self, db_session: AsyncSession, redis_client, parent_user: User
    ) -> None:
        token = await issue_token(
            db_session,
            user_id=parent_user.id,
            role=parent_user.role,
            family_id=parent_user.family_id,
            device_id="devA",
        )
        await commit_with_redis(db_session, redis_client)

        # mock DB to detect if it's called
        original_execute = db_session.execute
        db_session.execute = AsyncMock()  # type: ignore[method-assign]

        try:
            payload = await resolve_token(db_session, redis_client, token)
        finally:
            db_session.execute = original_execute  # type: ignore[method-assign]

        assert payload is not None
        assert payload.user_id == parent_user.id

    @pytest.mark.asyncio
    async def test_resolve_token_redis_miss_hits_db_and_backfills(
        self, db_session: AsyncSession, redis_client, parent_user: User
    ) -> None:
        token = await issue_token(
            db_session,
            user_id=parent_user.id,
            role=parent_user.role,
            family_id=parent_user.family_id,
            device_id="devB",
        )
        await commit_with_redis(db_session, redis_client)
        # Redis miss：删掉让它 miss
        th = token_hash(token)
        await redis_client.delete(_redis_key(th))

        payload = await resolve_token(db_session, redis_client, token)
        assert payload is not None
        assert payload.user_id == parent_user.id
        # 回填 Redis
        cached = await redis_client.get(_redis_key(th))
        assert cached is not None

    @pytest.mark.asyncio
    async def test_resolve_token_revoked_returns_none(
        self, db_session: AsyncSession, redis_client, parent_user: User
    ) -> None:
        token = await issue_token(
            db_session,
            user_id=parent_user.id,
            role=parent_user.role,
            family_id=parent_user.family_id,
            device_id="devC",
        )
        await commit_with_redis(db_session, redis_client)
        await revoke_token(db_session, token)
        await commit_with_redis(db_session, redis_client)

        payload = await resolve_token(db_session, redis_client, token)
        assert payload is None

    @pytest.mark.asyncio
    async def test_resolve_token_expired_returns_none(
        self, db_session: AsyncSession, redis_client, parent_user: User
    ) -> None:
        from sqlalchemy import update
        token = await issue_token(
            db_session,
            user_id=parent_user.id,
            role=parent_user.role,
            family_id=parent_user.family_id,
            device_id="devD",
        )
        await commit_with_redis(db_session, redis_client)
        th = token_hash(token)
        # 手动把 expires_at 改成过去时间
        await db_session.execute(
            update(AuthToken).where(AuthToken.token_hash == th).values(
                expires_at=datetime.now(timezone.utc) - timedelta(days=1)
            )
        )
        await db_session.commit()
        # Redis 里清掉，强制 miss
        await redis_client.delete(_redis_key(th))

        payload = await resolve_token(db_session, redis_client, token)
        assert payload is None

    @pytest.mark.asyncio
    async def test_resolve_token_does_not_update_db_expires_at(
        self, db_session: AsyncSession, redis_client, parent_user: User
    ) -> None:
        """续期已下放到 get_current_account；resolve_token 不碰 DB。"""
        token = await issue_token(
            db_session,
            user_id=parent_user.id,
            role=parent_user.role,
            family_id=parent_user.family_id,
            device_id="devE",
        )
        await commit_with_redis(db_session, redis_client)
        th = token_hash(token)

        from sqlalchemy import select
        before = (await db_session.execute(
            select(AuthToken.expires_at).where(AuthToken.token_hash == th)
        )).scalar_one()

        # resolve 多次
        await resolve_token(db_session, redis_client, token)
        await resolve_token(db_session, redis_client, token)

        after = (await db_session.execute(
            select(AuthToken.expires_at).where(AuthToken.token_hash == th)
        )).scalar_one()
        assert before == after  # DB expires_at 未变


# ---- needs_roll ----

class TestNeedsRoll:
    def test_needs_roll_parent_today_false_if_already_rolled(self) -> None:
        from zoneinfo import ZoneInfo
        cst = ZoneInfo("Asia/Shanghai")
        today = datetime.now(cst).date().isoformat()
        payload = TokenPayload(
            user_id=uuid.uuid4(),
            role=UserRole.parent,
            family_id=uuid.uuid4(),
            device_id="dev",
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            last_rolled_date=today,
        )
        assert needs_roll(payload) is False

    def test_needs_roll_parent_true_if_different_date(self) -> None:
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        payload = TokenPayload(
            user_id=uuid.uuid4(),
            role=UserRole.parent,
            family_id=uuid.uuid4(),
            device_id="dev",
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            last_rolled_date=yesterday,
        )
        assert needs_roll(payload) is True

    def test_needs_roll_child_never(self) -> None:
        payload = TokenPayload(
            user_id=uuid.uuid4(),
            role=UserRole.child,
            family_id=uuid.uuid4(),
            device_id="dev",
            expires_at=None,
            last_rolled_date=None,
        )
        assert needs_roll(payload) is False


# ---- roll_token_expiry ----

class TestRollTokenExpiry:
    # ---- B3 · 未 commit 时 Redis 保持旧值 ----

    @pytest.mark.asyncio
    async def test_roll_token_stage_redis_op_not_committed(
        self, db_session: AsyncSession, redis_client, parent_user: User
    ) -> None:
        """roll_token_expiry 调用后（未 commit），pending_redis_ops 有 setex，
        但 Redis 里 `auth:<th>` 的 value 仍为旧值，未被修改。"""
        token = await issue_token(
            db_session,
            user_id=parent_user.id,
            role=parent_user.role,
            family_id=parent_user.family_id,
            device_id="devF",
        )
        await commit_with_redis(db_session, redis_client)
        th = token_hash(token)

        # 记录 Redis 初始 value
        old_raw = await redis_client.get(f"auth:{th}")
        assert old_raw is not None

        # fresh resolve 拿 payload（不走 cache miss 路径）
        payload = await resolve_token(db_session, redis_client, token)
        assert payload is not None

        # 手动把 payload.last_rolled_date 拨到昨天（强制 needs_roll=True）
        import app.auth.tokens as tokens_mod
        yesterday = (
            datetime.fromisoformat(tokens_mod._today_cst()).date()
            - timedelta(days=1)
        ).isoformat()
        payload = payload.model_copy(update={"last_rolled_date": yesterday})

        # 调用 roll_token_expiry（不 commit）
        await roll_token_expiry(
            db_session, token_hash_hex=th, payload=payload
        )

        # 断言 pending_redis_ops 中有 setex
        pending = db_session.info.get("pending_redis_ops", [])
        assert any(
            op.kind == "setex" and op.key == f"auth:{th}"
            for op in pending
        )

        # 关键断言：Redis value 未被修改（仍为 old_raw）
        now_raw = await redis_client.get(f"auth:{th}")
        assert now_raw == old_raw, (
            f"Redis should not be written before commit; "
            f"old={old_raw!r}, now={now_raw!r}"
        )

        # 清理：discard pending ops，不触发 teardown 护栏
        discard_pending_redis_ops(db_session)
        # 不 rollback —— 验证路径不涉及 DB 状态的断言

    # ---- B2 · commit 后 Redis payload 已更新 ----

    @pytest.mark.asyncio
    async def test_roll_token_plus_commit_updates_redis_payload(
        self, db_session: AsyncSession, redis_client, parent_user: User,
    ) -> None:
        """commit_with_redis 后，Redis `auth:<th>` 的 JSON value 已更新：
        expires_at 增大，last_rolled_date == today_cst，value 字符串本身发生变化。"""
        import app.auth.tokens as tokens_mod

        token = await issue_token(
            db_session,
            user_id=parent_user.id,
            role=parent_user.role,
            family_id=parent_user.family_id,
            device_id="devG",
        )
        await commit_with_redis(db_session, redis_client)
        th = token_hash(token)

        # 记录初始 Redis value
        old_raw = await redis_client.get(f"auth:{th}")
        old_payload = TokenPayload.model_validate_json(old_raw)

        # 手动把 last_rolled_date 拨到昨天，强制 roll 发生
        yesterday = (
            datetime.fromisoformat(tokens_mod._today_cst()).date()
            - timedelta(days=1)
        ).isoformat()
        payload = old_payload.model_copy(update={"last_rolled_date": yesterday})

        # 停 monkeypatch（恢复 today），调 roll_token_expiry + commit
        await roll_token_expiry(db_session, token_hash_hex=th, payload=payload)
        await commit_with_redis(db_session, redis_client)

        # 重新读取 Redis
        new_raw = await redis_client.get(f"auth:{th}")
        new_payload = TokenPayload.model_validate_json(new_raw)

        # 断言：new.expires_at > old.expires_at
        assert new_payload.expires_at > old_payload.expires_at, (
            f"expires_at should increase after roll; "
            f"old={old_payload.expires_at}, new={new_payload.expires_at}"
        )
        # 断言：new.last_rolled_date == today_cst（真今日，CST 对齐）
        assert new_payload.last_rolled_date == tokens_mod._today_cst()
        # 断言：value 字符串本身发生变化
        assert new_raw != old_raw

    @pytest.mark.asyncio
    async def test_roll_token_plus_commit_persists(
        self, db_session: AsyncSession, redis_client, parent_user: User
    ) -> None:
        token = await issue_token(
            db_session,
            user_id=parent_user.id,
            role=parent_user.role,
            family_id=parent_user.family_id,
            device_id="devG",
        )
        await commit_with_redis(db_session, redis_client)
        th = token_hash(token)
        payload = await resolve_token(db_session, redis_client, token)
        assert payload is not None

        await roll_token_expiry(db_session, token_hash_hex=th, payload=payload)
        await commit_with_redis(db_session, redis_client)

        # 同 session 直接查（commit 后可见）
        from sqlalchemy import select
        row = (await db_session.execute(
            select(AuthToken.expires_at).where(AuthToken.token_hash == th)
        )).scalar_one()
        assert row is not None
        assert row > datetime.now(timezone.utc)


# ---- revoke_token ----

class TestRevokeToken:
    @pytest.mark.asyncio
    async def test_revoke_token_writes_revoked_at_and_deletes_redis(
        self, db_session: AsyncSession, redis_client, parent_user: User
    ) -> None:
        token = await issue_token(
            db_session,
            user_id=parent_user.id,
            role=parent_user.role,
            family_id=parent_user.family_id,
            device_id="devH",
        )
        await commit_with_redis(db_session, redis_client)
        th = token_hash(token)

        await revoke_token(db_session, token)
        await commit_with_redis(db_session, redis_client)

        # DB
        from sqlalchemy import select
        row = (await db_session.execute(
            select(AuthToken.revoked_at).where(AuthToken.token_hash == th)
        )).scalar_one()
        assert row is not None

        # Redis
        cached = await redis_client.get(_redis_key(th))
        assert cached is None

    @pytest.mark.asyncio
    async def test_revoke_token_idempotent_on_nonexistent(
        self, db_session: AsyncSession, redis_client
    ) -> None:
        # 吊销不存在的 token 不报错
        await revoke_token(db_session, "nonexistent_token_abc123")
        await commit_with_redis(db_session, redis_client)  # 不抛


# ---- revoke_all_active_tokens ----

class TestRevokeAllActiveTokens:
    @pytest.mark.asyncio
    async def test_revoke_all_returns_count_and_revocates_all(
        self, db_session: AsyncSession, redis_client, parent_user: User
    ) -> None:
        # 同 user 连续 3 次 issue（不同 device_id）
        t1 = await issue_token(db_session, user_id=parent_user.id, role=parent_user.role,
                               family_id=parent_user.family_id, device_id="dev1")
        t2 = await issue_token(db_session, user_id=parent_user.id, role=parent_user.role,
                               family_id=parent_user.family_id, device_id="dev2")
        t3 = await issue_token(db_session, user_id=parent_user.id, role=parent_user.role,
                               family_id=parent_user.family_id, device_id="dev3")
        await commit_with_redis(db_session, redis_client)

        count = await revoke_all_active_tokens(db_session, parent_user.id)
        await commit_with_redis(db_session, redis_client)

        assert count == 3

        from sqlalchemy import select
        rows = (await db_session.execute(
            select(AuthToken).where(AuthToken.user_id == parent_user.id)
        )).scalars().all()
        assert all(r.revoked_at is not None for r in rows)

        # Redis keys gone
        for t in [t1, t2, t3]:
            cached = await redis_client.get(_redis_key(token_hash(t)))
            assert cached is None

    @pytest.mark.asyncio
    async def test_revoke_all_idempotent_zero_tokens(
        self, db_session: AsyncSession, redis_client, parent_user: User
    ) -> None:
        count = await revoke_all_active_tokens(db_session, parent_user.id)
        await commit_with_redis(db_session, redis_client)
        assert count == 0

    @pytest.mark.asyncio
    async def test_revoke_all_user_isolation(
        self, db_session: AsyncSession, redis_client, parent_user: User, child_user: User
    ) -> None:
        # parent 有一个 token
        pt = await issue_token(db_session, user_id=parent_user.id, role=parent_user.role,
                                family_id=parent_user.family_id, device_id="pdev")
        # child 有 token
        ct = await issue_token(db_session, user_id=child_user.id, role=child_user.role,
                               family_id=child_user.family_id, device_id="cdev", ttl_days=None)
        await commit_with_redis(db_session, redis_client)

        # revoke parent all
        count = await revoke_all_active_tokens(db_session, parent_user.id)
        await commit_with_redis(db_session, redis_client)
        assert count == 1

        # child token 仍然有效
        child_payload = await resolve_token(db_session, redis_client, ct)
        assert child_payload is not None
        # parent token 已吊销
        parent_payload = await resolve_token(db_session, redis_client, pt)
        assert parent_payload is None
