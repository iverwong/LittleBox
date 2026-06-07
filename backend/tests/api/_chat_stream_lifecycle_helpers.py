"""Shared scaffolding for chat stream lifecycle tests.

Provides fixtures that use real db_session_factory (not MagicMock) for
segment 1 bg task, backed by concurrent_db_sessions for TRUNCATE cleanup.

Usage (典型模式)::

    async def test_xxx(lifecycle_ctx):
        ctx = lifecycle_ctx  # SimpleNamespace
        # 1. Seed data with ctx.seed_sess
        # 2. POST via ctx.client
        # 3. Assert with ctx.assert_sess or ctx.redis_client
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from app.core.config import settings as _module_settings
from app.core.db import get_db
from app.core.enums import UserRole
from app.core.redis import get_redis
from app.core.runtime import RuntimeResources
from app.main import create_app
from app.models.accounts import User
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

TABLES = [
    "families", "users", "family_members", "child_profiles",
    "auth_tokens", "device_tokens", "sessions", "messages",
]

# 模块级 FakeRedis.eval patch（特化版：监控各测试文件传参）
_original_fake_eval = None


def _patch_fakeredis_eval():
    """Patch FakeRedis.eval to simulate Lua DEL-if-nonce-match.

    Returns the original eval function for later restoration.
    """
    import fakeredis.aioredis

    async def mock_eval(self, script: str, num_keys: int, key: str, nonce_arg: str) -> int:  # noqa: N805
        stored = await self.get(key)
        if stored == nonce_arg:
            await self.delete(key)
            return 1
        return 0

    global _original_fake_eval
    _original_fake_eval = fakeredis.aioredis.FakeRedis.eval
    fakeredis.aioredis.FakeRedis.eval = mock_eval


def _restore_fakeredis_eval():
    """Restore the original FakeRedis.eval."""
    import fakeredis.aioredis

    global _original_fake_eval
    if _original_fake_eval is not None:
        fakeredis.aioredis.FakeRedis.eval = _original_fake_eval


@pytest_asyncio.fixture
async def lifecycle_ctx(engine, redis_client, concurrent_db_sessions):
    """Fixture providing complete lifecycle test context.

    Returns SimpleNamespace with:
    - client: httpx AsyncClient (real db_session_factory + eval-patched fakeredis)
    - assert_sess / seed_sess: AsyncSession from concurrent_db_sessions
    - rr: real RuntimeResources
    - redis_client: FakeRedis (eval-patched)
    - make_sessions: concurrent_db_sessions._make for additional sessions
    """
    # ---- Patch FakeRedis.eval for all lifecycle tests ----
    _patch_fakeredis_eval()
    try:
        # ---- Obtain sessions from concurrent_db_sessions ----
        sessions = await concurrent_db_sessions(count=2, tables=TABLES)
        seed_sess, assert_sess = sessions[0], sessions[1]

        # ---- Build app with real db_session_factory ----
        _factory = async_sessionmaker(engine, expire_on_commit=False)

        app = create_app()

        async def _get_db_override():
            """Override get_db: yield a real session (not savepoint wrapper).

            Without an outer begin(), the handler's db.commit() does a real
            PG commit, making data visible to segment 1's session.
            """
            sess = _factory()
            try:
                yield sess
            finally:
                await sess.close()

        async def _get_redis_override():
            return redis_client

        app.dependency_overrides[get_db] = _get_db_override
        app.dependency_overrides[get_redis] = _get_redis_override

        # ---- Build runtime with real db_session_factory ----
        # 注意：rr 的 _chat_tasks 由 field(default_factory=dict) 初始化为 {}
        # chat_queue_maxsize 默认 128（覆盖在调用侧通过 model_copy 实现）
        rr = RuntimeResources(
            settings=_module_settings,
            db_engine=engine,
            db_session_factory=_factory,
            audit_redis=redis_client,
            arq_pool=AsyncMock(),
            main_graph=MagicMock(),
            audit_graph=MagicMock(),
        )
        app.state.resources = rr

        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://testserver")

        ctx = SimpleNamespace(
            client=client,
            assert_sess=assert_sess,
            seed_sess=seed_sess,
            rr=rr,
            redis_client=redis_client,
            app=app,
        )
        try:
            yield ctx
        finally:
            await client.aclose()
            app.dependency_overrides.clear()
    finally:
        _restore_fakeredis_eval()


async def seed_child_user(sess) -> User:
    """Seed a child user + family + ChildProfile (委托 conftest 的共享 helper)。

    Returns the created User (committed)。
    """
    from tests.conftest import make_child_user_with_profile

    return await make_child_user_with_profile(sess)


async def make_auth_headers(sess, redis_client, user) -> dict:
    """Issue a child auth token and return auth headers dict."""
    from app.auth.tokens import issue_token
    from app.core.redis import commit_with_redis

    device_id = "test-device-lifecycle"
    token = await issue_token(
        sess,
        user_id=user.id,
        role=UserRole.child,
        family_id=user.family_id,
        device_id=device_id,
        ttl_days=None,
    )
    await commit_with_redis(sess, redis_client)
    return {
        "Authorization": f"Bearer {token}",
        "X-Device-Id": device_id,
    }


async def lifecycle_setup(ctx) -> tuple:
    """Quick setup helper: seed child user + return (client, headers, child_user).

    Usage in migrated tests::

        client, headers, child = await lifecycle_setup(ctx)
    """
    child = await seed_child_user(ctx.seed_sess)
    headers = await make_auth_headers(ctx.seed_sess, ctx.redis_client, child)
    return ctx.client, headers, child


async def seed_compression_session(ctx, child) -> tuple:
    """Seed a session with 2 active messages + needs_compression=True.

    Returns (sid, child, msg1_id, msg2_id) matching compression_session fixture.
    """
    from datetime import UTC
    from datetime import datetime as _dt
    from uuid import uuid4 as _uuid4

    from app.core.enums import MessageRole, MessageStatus
    from app.models.chat import Message
    from app.models.chat import Session as SessionModel

    base_ts = _dt.now(UTC)
    sid = _uuid4()
    session = SessionModel(id=sid, child_user_id=child.id, title="test")
    ctx.seed_sess.add(session)
    await ctx.seed_sess.flush()

    msg1 = Message(
        session_id=sid, role=MessageRole.human,
        content="你好", status=MessageStatus.active,
    )
    msg1.created_at = base_ts
    ctx.seed_sess.add(msg1)
    await ctx.seed_sess.flush()

    msg2 = Message(
        session_id=sid, role=MessageRole.ai,
        content="今天天气不错", status=MessageStatus.active,
    )
    msg2.created_at = base_ts.replace(microsecond=base_ts.microsecond + 1)
    ctx.seed_sess.add(msg2)
    await ctx.seed_sess.flush()

    session.needs_compression = True
    session.context_size_tokens = 600000
    await ctx.seed_sess.commit()
    return sid, child, msg1.id, msg2.id
