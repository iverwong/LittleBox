"""M4 新增：Auth + DB 测试基础设施。

设计要点：
- DB：真 PostgreSQL，独立 `littlebox_test` 库；session 开始 DROP/CREATE + alembic upgrade head；
  function 每测试外层 transaction + nested savepoint，
  业务 `session.commit()` 实际 release savepoint，
  teardown rollback 外层 → 零持久化 → 测试完全隔离
- Redis：fakeredis 进程内模拟，每测试独立实例
- FastAPI：`dependency_overrides` 注入 `get_db` / `get_redis` 指向测试 fixture
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest_asyncio
from alembic.config import Config
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.auth.redis_client import get_redis
from app.config import settings
from app.db import get_db
from app.main import create_app

TEST_DB_NAME = "littlebox_test"


def _base_url() -> str:
    """从 settings.database_url 派生，保证 host / port / 凭证 与开发一致。
    用 render_as_string(hide_password=False)：SQLAlchemy URL 的 __str__ 默认会把
    密码遮蔽成 ***，create_async_engine 拿到 *** 直接触发 asyncpg 密码认证失败。
    详见决策背景 §11.1。"""
    return make_url(settings.database_url).render_as_string(hide_password=False)


def _admin_url() -> str:
    # postgres 镜像默认存在的维护库；用于 DROP/CREATE 测试库
    return (
        make_url(settings.database_url)
        .set(database="postgres")
        .render_as_string(hide_password=False)
    )


def _test_url() -> str:
    return (
        make_url(settings.database_url)
        .set(database=TEST_DB_NAME)
        .render_as_string(hide_password=False)
    )


# ---------- session scope：建库 + migration ----------


@pytest_asyncio.fixture(scope="session")
async def _bootstrap_test_db() -> AsyncGenerator[None, None]:
    """每跑一轮测试：断开测试库残留连接 → DROP → CREATE → alembic upgrade head。"""
    # TODO(xdist): 开 pytest-xdist 时按 worker 隔离库名（TEST_DB_NAME + worker id）
    admin_engine = create_async_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = '{TEST_DB_NAME}' AND pid <> pg_backend_pid()"
                )
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
            await conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    finally:
        await admin_engine.dispose()

    # alembic command.upgrade 是同步 API，内部会起自己的 loop 跑 async env.py。
    # pytest 已在 session loop 中，用 executor 隔离避免 loop 冲突。
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _test_url())
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, command.upgrade, cfg, "head")

    yield
    # 不主动 drop：保留库以便 CI 失败后下载骨现场；下次 session 头会重建


@pytest_asyncio.fixture
async def engine(_bootstrap_test_db: None) -> AsyncGenerator[AsyncEngine, None]:
    # engine 必须是 function scope：asyncpg 的连接池以 event loop 为 key。
    # session scope engine 在 pytest-asyncio 的 loop 管理下会导致连接被错误 loop
    # 的 Future 引用，引发 "attached to a different loop" 错误。
    # NullPool 保证每次 connect() 创建 fresh 连接、close() 立即销毁，
    # 配合 function scope 实现真正独立的连接实例。
    eng = create_async_engine(_test_url(), poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


# ---------- function scope：每测试 savepoint 隔离 ----------


@pytest_asyncio.fixture
async def db_session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """外层 connection + begin；注入 session 时 `join_transaction_mode="create_savepoint"`。
    业务代码 `await session.commit()` 实际只 release 内层 savepoint，不会落盘。
    teardown 外层 rollback → 本测试所有写入全部丢弃。
    """
    async with engine.connect() as connection:
        trans = await connection.begin()
        session = AsyncSession(
            bind=connection,
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,  # savepoint release 后仍可访问 ORM 属性
        )
        try:
            yield session
        finally:
            # 护栏：漏调 commit_with_redis 的测试会在此抛 AssertionError（§8 封装）
            pending = session.info.get("pending_redis_ops") or []
            await session.close()
            if trans.is_active:
                await trans.rollback()
            assert not pending, (
                "pending redis ops not flushed — use commit_with_redis() "
                "instead of bare db.commit()"
            )


# ---------- function scope：fakeredis ----------


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[FakeRedis, None]:
    client = FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


# ---------- function scope：FastAPI ASGI client ----------


@pytest_asyncio.fixture
async def app(db_session: AsyncSession, redis_client: FakeRedis) -> AsyncGenerator[FastAPI, None]:
    application = create_app()

    async def _get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def _get_redis() -> FakeRedis:
        return redis_client

    application.dependency_overrides[get_db] = _get_db
    application.dependency_overrides[get_redis] = _get_redis
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def api_client(app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# ---------- 业务高层便捷 fixtures（后续 Step 复用） ----------

@pytest_asyncio.fixture
async def seeded_parent(db_session: AsyncSession) -> tuple[User, str]:
    """种一个 active parent + family + family_members。返回 (user, plaintext_password)。"""
    from app.auth.password import generate_password, generate_phone, hash_password
    from app.models.accounts import Family, FamilyMember, User
    from app.models.enums import UserRole

    pw = generate_password()
    fam = Family()
    db_session.add(fam)
    await db_session.flush()

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
    await db_session.commit()  # 实际 release savepoint
    return user, pw


@pytest_asyncio.fixture
async def inactive_parent(db_session: AsyncSession) -> tuple[User, str]:
    """种一个 is_active=False 的 parent。返回 (user, plaintext_password)。"""
    from app.auth.password import generate_password, generate_phone, hash_password
    from app.models.accounts import Family, FamilyMember, User
    from app.models.enums import UserRole

    pw = generate_password()
    fam = Family()
    db_session.add(fam)
    await db_session.flush()

    user = User(
        family_id=fam.id,
        role=UserRole.parent,
        phone=generate_phone(),
        password_hash=hash_password(pw),
        is_active=False,
    )
    db_session.add(user)
    await db_session.flush()

    db_session.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.parent))
    await db_session.commit()
    return user, pw


@pytest_asyncio.fixture
async def child_user(db_session: AsyncSession) -> User:
    """种一个 child + family（无 password_hash）。"""
    from app.models.accounts import Family, FamilyMember, User
    from app.models.enums import UserRole

    fam = Family()
    db_session.add(fam)
    await db_session.flush()

    user = User(
        family_id=fam.id,
        role=UserRole.child,
        phone="0000",  # 固定 phone，避免随机生成与 seeded_parent 冲突
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    db_session.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.child))
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def rate_limit_parent(db_session: AsyncSession) -> tuple[User, str]:
    """种一个固定 phone='abcd' 的 active parent，用于 rate-limit 计数测试。"""
    from app.auth.password import generate_password, hash_password
    from app.models.accounts import Family, FamilyMember, User
    from app.models.enums import UserRole

    pw = generate_password()
    fam = Family()
    db_session.add(fam)
    await db_session.flush()

    user = User(
        family_id=fam.id,
        role=UserRole.parent,
        phone="abcd",  # 固定 phone，用于 rate-limit 测试
        password_hash=hash_password(pw),
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    db_session.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.parent))
    await db_session.commit()
    return user, pw
