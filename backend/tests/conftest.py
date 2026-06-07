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
      concurrent_db_sessions (N 独立 AsyncSession, 真 commit + TRUNCATE 清空，
      用于真并发验证场景。与 db_session 互斥使用。)
- HTTP: api_client (ASGI in-process)
- Redis: redis_client (fakeredis, 作用域 function)

禁止:
- subprocess 跑 `app.scripts.*` 连真实库
- httpx 直连真 server (localhost:8000 等)
- redis.Redis(...) 显式连真实 host
- from app.core.config import settings 后用 settings.database_url 自建 engine
- flushdb() / flushall()
- db_session 与 concurrent_db_sessions 混用（savepoint 语义不兼容）

双层运行时防御:
- 模块级 _test_url() 断言（本文件顶部）
- session 级 _prod_db_row_count_guard fixture

历史教训: M6-patch · 测试隔离纪律加固
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from collections.abc import AsyncGenerator

# M8 audit pipeline test defaults
# 必须早于 from app.core.config import settings，否则 Settings() 实例化时 env 已读完
os.environ.setdefault("LB_AUDIT_PROVIDER", "deepseek")
os.environ.setdefault("LB_AUDIT_MODEL", "deepseek-v4-flash")
os.environ.setdefault("LB_AUDIT_REASONING_EFFORT", "max")
os.environ.setdefault("LB_AUDIT_THINKING_ENABLED", "True")
os.environ.setdefault("LB_AUDIT_WAIT_TIMEOUT_SECONDS", "30")
os.environ.setdefault("LB_AUDIT_REDIS_TTL_SECONDS", "86400")
os.environ.setdefault("LB_ARQ_REDIS_DB", "1")
os.environ.setdefault("LB_MAX_AUDIT_TOOL_ITERATIONS", "5")

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis


def pytest_addoption(parser):
    parser.addoption(
        "--run-live", action="store_true", default=False,
        help="run tests marked as live (real LLM API)",
    )
    parser.addoption(
        "--run-integration", action="store_true", default=False,
        help="run integration tests (real DB/Redis/arq)",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-live"):
        skip_live = pytest.mark.skip(reason="需要 --run-live 显式触发")
        for item in items:
            if item.get_closest_marker("live"):
                item.add_marker(skip_live)
    if not config.getoption("--run-integration"):
        skip_int = pytest.mark.skip(reason="需要 --run-integration 显式触发")
        for item in items:
            if item.get_closest_marker("integration"):
                item.add_marker(skip_int)
from app.core.config import settings
from app.core.db import get_db
from app.core.redis import get_redis
from app.main import create_app
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

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


# ---------- function scope：真并发验证（独立 connection + TRUNCATE） ----------


@pytest_asyncio.fixture
async def concurrent_db_sessions(
    request: pytest.FixtureRequest,
    engine: AsyncEngine,
) -> AsyncGenerator:
    """提供 N 个独立 connection 的 AsyncSession，用于真并发场景验证。

    与 db_session fixture 互斥使用：db_session 走 savepoint + outer rollback
    （逻辑清空），本 fixture 走真 commit + TRUNCATE（物理清空）。混用会语义错乱。

    使用示例:
        sessions = await concurrent_db_sessions(
            count=5,
            tables=["sessions", "messages", "users", "families"],
        )
        async def worker(db): ...
        await asyncio.gather(*[worker(s) for s in sessions])
    """
    if "db_session" in request.fixturenames:
        pytest.fail(
            "concurrent_db_sessions 与 db_session 互斥使用 — "
            "前者真 commit + TRUNCATE，后者 savepoint + rollback"
        )

    db_name = str(engine.url.database or "")
    assert "_test" in db_name, (
        f"concurrent_db_sessions 仅可用于测试库（库名须含 '_test'），"
        f"实际连到 {db_name}"
    )

    created_sessions: list[AsyncSession] = []
    dirtied_tables: list[str] = []

    async def _make(count: int, tables: list[str]) -> list[AsyncSession]:
        nonlocal dirtied_tables
        if dirtied_tables:
            raise RuntimeError("concurrent_db_sessions 不支持同一测试内多次调用 _make")
        if not tables:
            raise ValueError("tables 不可为空")
        dirtied_tables = list(tables)
        for _ in range(count):
            session = AsyncSession(engine, expire_on_commit=False)
            created_sessions.append(session)
        return created_sessions

    try:
        yield _make
    finally:
        for s in created_sessions:
            try:
                await s.close()
            except Exception:
                pass
        if dirtied_tables:
            tables_sql = ", ".join(f'"{t}"' for t in dirtied_tables)
            async with engine.connect() as conn:
                await conn.execute(
                    text(f"TRUNCATE TABLE {tables_sql} RESTART IDENTITY CASCADE")
                )
                await conn.commit()


# ---------- function scope：fakeredis ----------


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[FakeRedis, None]:
    client = FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


# ---------- function scope：FastAPI ASGI client ----------


def _inject_mock_resources(application: FastAPI, redis_client: FakeRedis) -> None:
    """为 app 注入 mock RuntimeResources + 禁用 lifespan（避免 Redis 连接 hang）。

    M9 Step 10: main_graph 直接用 MagicMock（不走模块级 _main_graph）。
    """
    mock_rr = _make_mock_resources(redis_client)
    application.state.resources = mock_rr
    application.router.lifespan_context = lambda _: contextlib.nullcontext()


def _make_mock_resources(redis_client: FakeRedis):
    """创建测试用 mock RuntimeResources，避免 build_runtime 连接真实 Redis/DB。"""
    from unittest.mock import AsyncMock, MagicMock

    from app.core.runtime import RuntimeResources

    mock_rr = MagicMock(spec=RuntimeResources)
    mock_rr.main_graph = MagicMock()
    mock_rr.main_graph.astream = AsyncMock()
    mock_rr.audit_graph = MagicMock()
    mock_rr.settings = MagicMock()
    mock_rr.settings.main_provider = "deepseek"
    mock_rr.settings.compression_provider = "deepseek"
    mock_rr.settings.deepseek_api_key.get_secret_value.return_value = ""
    mock_rr.db_session_factory = MagicMock()
    mock_rr.audit_redis = redis_client
    mock_rr.arq_pool = AsyncMock()
    mock_rr.arq_pool.close = AsyncMock()
    mock_rr.db_engine = AsyncMock()
    mock_rr.db_engine.dispose = AsyncMock()
    return mock_rr


@pytest_asyncio.fixture
async def app(db_session: AsyncSession, redis_client: FakeRedis) -> AsyncGenerator[FastAPI, None]:
    application = create_app()

    async def _get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def _get_redis() -> FakeRedis:
        return redis_client

    application.dependency_overrides[get_db] = _get_db
    application.dependency_overrides[get_redis] = _get_redis

    _inject_mock_resources(application, redis_client)

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
    from app.core.enums import UserRole
    from app.domain.auth.password import generate_password, generate_phone, hash_password
    from app.models.accounts import Family, FamilyMember, User

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
    from app.core.enums import UserRole
    from app.domain.auth.password import generate_password, generate_phone, hash_password
    from app.models.accounts import Family, FamilyMember, User

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


async def make_child_user_with_profile(sess: AsyncSession):
    """种一个 child + family + ChildProfile (commit 后返回 User)。

    M4 创建流程强绑定 child_profile (fix me-childprofile-404 后, 缺失即 404,
    不再静默兜底), 故任何需要走 chat stream 路由的测试都必须经本函数
    种出 ChildProfile。pytest fixture `child_user` 与 lifecycle helper
    `seed_child_user` 都委托给本函数, 避免 fixture 重复。
    """
    from datetime import date

    from app.core.enums import Gender, UserRole
    from app.models.accounts import ChildProfile, Family, FamilyMember, User

    fam = Family()
    sess.add(fam)
    await sess.flush()

    # child 不设 phone —— User.phone 注释明确"仅父账号"。
    # 用 None 让 partial unique index `users.phone` (WHERE role='parent' AND
    # is_active=true) 不会误伤, 也避免与 seeded_parent 假 phone 撞车。
    user = User(
        family_id=fam.id,
        role=UserRole.child,
        is_active=True,
    )
    sess.add(user)
    await sess.flush()

    sess.add(FamilyMember(family_id=fam.id, user_id=user.id, role=UserRole.child))
    sess.add(
        ChildProfile(
            child_user_id=user.id,
            created_by=user.id,
            birth_date=date(2015, 1, 1),
            gender=Gender.male,
            nickname="test",
        )
    )
    await sess.commit()
    return user


@pytest_asyncio.fixture
async def child_user(db_session: AsyncSession):
    """种一个 child + family + ChildProfile (委托 make_child_user_with_profile)。"""
    return await make_child_user_with_profile(db_session)


@pytest_asyncio.fixture
async def rate_limit_parent(db_session: AsyncSession) -> tuple:
    """种一个固定 phone='abcd' 的 active parent，用于 rate-limit 计数测试。"""
    from app.core.enums import UserRole
    from app.domain.auth.password import generate_password, hash_password
    from app.models.accounts import Family, FamilyMember, User

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
    "families",
    "users",
    "child_profiles",
    "auth_tokens",
    "device_tokens",
    "family_members",
    "sessions",
    "messages",
    "audit_records",
    "rolling_summaries",
    "daily_reports",
    "notifications",
    "data_deletion_requests",
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
