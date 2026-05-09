"""M4 新增：Auth + DB 测试基础设施。

设计要点：
- DB：真 PostgreSQL，独立 `littlebox_test` 库；session 开始 DROP/CREATE + alembic upgrade head；
  function 每测试外层 transaction + nested savepoint，
  业务 `session.commit()` 实际 release savepoint，
  teardown rollback 外层 → 零持久化 → 测试完全隔离
- Redis：fakeredis 进程内模拟，每测试独立实例
- FastAPI：`dependency_overrides` 注入 `get_db` / `get_redis` 指向测试 fixture

测试隔离铁律（M6-patch 后强制纪律）：
所有涉及 DB / Redis 的测试**必须**通过本文件的 fixture 进入:
- DB: db_session (savepoint rollback, 作用域 function)
- HTTP: api_client (ASGI in-process)
- Redis: redis_client (fakeredis, 作用域 function)

禁止:
- subprocess 跑 `app.scripts.*` 连真实库
- httpx 直连真 server (localhost:8000 等)
- redis.Redis(...) 显式连真实 host
- from app.config import settings 后用 settings.database_url 自建 engine
- flushdb() / flushall()

双层运行时防御:
- 模块级 _test_url() 断言（本文件顶部）
- session 级 _prod_db_row_count_guard fixture

历史教训: M6-patch · 测试隔离纪律加固
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth.redis_client import get_redis
from app.config import settings
from app.db import get_db
from app.main import create_app

TEST_DB_NAME = "littlebox_test"


def _base_url() -> str:
    """从 settings.database_url 派生，保证 host / port / 凭证 与开发一致。"""
    return make_url(settings.database_url).render_as_string(hide_password=False)


def _admin_url() -> str:
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


# ---------- 模块级 fail-fast 断言 ----------
#  放在 _test_url() 定义之后，确保函数已就绪
_RESOLVED_TEST_URL = _test_url()
assert "_test" in make_url(_RESOLVED_TEST_URL).database, (
    f"FATAL: 测试库 URL 数据库名必须含 '_test', 实际 {_RESOLVED_TEST_URL}。"
    f"请检查 TEST_DB_NAME 是否误删 '_test' 后缀。"
)


# ---------- session scope：建库 + migration ----------


@pytest_asyncio.fixture(scope="session")
async def _bootstrap_test_db() -> AsyncGenerator[None, None]:
    """每跑一轮测试：断开测试库残留连接 → DROP → CREATE → alembic upgrade head。

    使用 subprocess 调 alembic CLI：绕开 pytest-asyncio event loop 与 env.py 中
    asyncio.run() 的冲突。alembic 进程有独立 Python 解释器，不共享 pytest 的 event loop。
    """
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

    # subprocess 调 alembic：alembic.ini 通过 settings 读取 LB_DATABASE_URL，
    # 在 env 中显式传入 test DB URL 以覆盖默认值。
    test_db_url = _test_url()
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        env={**os.environ, "LB_DATABASE_URL": test_db_url},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade head failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

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
async def seeded_parent(db_session: AsyncSession) -> tuple:
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
async def inactive_parent(db_session: AsyncSession) -> tuple:
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
async def child_user(db_session: AsyncSession):
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
async def rate_limit_parent(db_session: AsyncSession) -> tuple:
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
        phone="abcd",
        password_hash=hash_password(pw),
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    db_session.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.parent))
    await db_session.commit()
    return user, pw


# ---------- session scope：真库行数兜底（M6-patch T4 防御） ----------

_GUARD_TABLES = [
    "users", "families", "family_members",
    "data_deletion_requests", "notifications",
]


async def _async_count_rows(url: str, tables: list[str]) -> dict[str, int]:
    from sqlalchemy import text as _text
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return {
                t: (await conn.execute(_text(f"SELECT COUNT(*) FROM {t}"))).scalar_one()
                for t in tables
            }
    finally:
        await engine.dispose()


def _count_rows(url: str, tables: list[str]) -> dict[str, int]:
    """同步调用 async COUNT 查询。asyncio.run() 创建独立事件循环。"""
    return asyncio.run(_async_count_rows(url, tables))


@pytest.fixture(scope="session", autouse=True)
def _prod_db_row_count_guard():
    """启动记录真库 baseline, session 结束比对。任一目标表行数变化即 fail。"""
    if os.getenv("LB_SKIP_PROD_GUARD") == "1":
        yield
        return

    prod_url = settings.database_url
    baseline = _count_rows(prod_url, _GUARD_TABLES)
    yield
    final = _count_rows(prod_url, _GUARD_TABLES)
    diffs = {t: (baseline[t], final[t]) for t in _GUARD_TABLES if baseline[t] != final[t]}
    assert not diffs, f"FATAL: 真库行数变化(测试污染): {diffs}"
