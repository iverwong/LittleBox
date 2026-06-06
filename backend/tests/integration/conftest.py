"""集成测试基建（M9.5 Step 2–6）。

本 conftest 覆盖根 conftest 的 app/redis_client fixture，使用真 DB + 真 Redis + 真 arq worker。

隔离铁律豁免声明：
  - flushdb 仅在 integration_redis fixture 内使用，仅操作 Redis db index 15
  - truncate_tables 仅在 integration_engine 上操作，仅操作 littlebox_integration 库
  - 以上为 _prod_db_row_count_guard 的唯一豁免点（因集成库与生产库物理隔离）

设计要点：
  - 集成库 littlebox_integration：session 级 DROP/CREATE + alembic upgrade head，
    测试间 TRUNCATE 清理（真 commit 语义，不走 savepoint）
  - Redis db 15：所有 Redis 操作（chat:lock、chat:throttle、arq 队列、audit:*）
    收敛到同一 db index，确保 enqueue/drain 一致性
  - IntegrationSettings：覆写 database_url + redis_url + arq_redis_db，
    使 build_runtime 构建的 RuntimeResources 全部指向隔离实例
  - in-process arq worker：使用 WORKER_SETTINGS["functions"] 字符串路径注册，
    override on_startup 注入集成 RuntimeResources
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Callable
from typing import Any
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings

INTEGRATION_DB_NAME = "littlebox_integration"
INTEGRATION_REDIS_DB = 15

# ---------- URL helpers ----------


def _admin_url() -> str:
    """连 postgres 维护库（无数据库名），用于 DROP/CREATE。"""
    return make_url(Settings().database_url).set(database="postgres").render_as_string(
        hide_password=False,
    )


def _integration_db_url() -> str:
    return make_url(Settings().database_url).set(database=INTEGRATION_DB_NAME).render_as_string(
        hide_password=False,
    )


def _integration_redis_url() -> str:
    parsed = urlparse(Settings().redis_url)
    return urlunparse(parsed._replace(path=f"/{INTEGRATION_REDIS_DB}"))


# ---------- IntegrationSettings ----------


def build_integration_settings() -> Settings:
    """构建集成测试 Settings：所有资源指向隔离实例。

    关注点 1（Redis db 一致性闭环）：
      redis_url → db 15（承载 chat:lock / chat:throttle / get_redis）
      arq_redis_db → 15（_build_arq_redis_url 派生 audit_redis 也指向 db 15，
                        arq pool RedisSettings(database=15) 也指向 db 15）
    三方 pool 全部收敛到同一 db index。
    """
    return Settings(
        database_url=_integration_db_url(),
        redis_url=_integration_redis_url(),
        arq_redis_db=INTEGRATION_REDIS_DB,
    )


# ---------- Step 2: Session-scoped DB bootstrap ----------


@pytest_asyncio.fixture(scope="session")
async def _bootstrap_integration_db() -> AsyncGenerator[None, None]:
    """DROP/CREATE littlebox_integration + alembic upgrade head。

    与根 conftest _bootstrap_test_db 同模式，独立数据库名。
    使用 subprocess 调 alembic CLI 避免 event loop 冲突。
    """
    import subprocess

    admin_engine = create_async_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = '{INTEGRATION_DB_NAME}' AND pid <> pg_backend_pid()"
                )
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{INTEGRATION_DB_NAME}"'))
            await conn.execute(text(f'CREATE DATABASE "{INTEGRATION_DB_NAME}"'))
    finally:
        await admin_engine.dispose()

    test_db_url = _integration_db_url()
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
    # 不主动 drop：保留库以便调试；下次 session 头重建


# ---------- Session-scoped row count guard (M9.5 计划偏差登记) ----------
# 计划字面要求"纳入 _prod_db_row_count_guard"，但根 conftest 的 guard 只对
# settings.database_url（生产库）做 baseline/final 比对。集成库 littlebox_integration
# 是独立物理库，自然不受该 guard 管辖，且每次 session 头 DROP/CREATE + alembic 重建，
# 不留持久状态。因此"行数兜底"目标通过物理隔离 + 自建 guard 实现等效保障，
# 而非在根 conftest 中追加 integration 库的表名。
# 下面 session 末断言确保集成库在各测试间 TRUNCATE 后无跨 session 泄漏。


@pytest.fixture(scope="session", autouse=True)
def _integration_row_count_guard():
    """集成库行数兜底：session 末 13 表应为空（被 truncate_tables 确保）。

    若触发失败，说明有测试未使用 truncate_tables（或该 fixture 未生效）。
    """
    yield

    import asyncio
    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _check():
        engine = create_async_engine(_integration_db_url())
        try:
            async with engine.connect() as conn:
                for t in _GUARD_TABLES:
                    cnt = (await conn.execute(_text(f"SELECT COUNT(*) FROM {t}"))).scalar_one()
                    assert cnt == 0, (
                        f"集成库表 {t} 残留 {cnt} 行——truncate_tables 未覆盖或未生效"
                    )
        finally:
            await engine.dispose()

    asyncio.run(_check())


# ---------- Step 2: Integration engine (for TRUNCATE) ----------


@pytest_asyncio.fixture
async def _integration_engine(
    _bootstrap_integration_db: None,
) -> AsyncGenerator[AsyncEngine, None]:
    """集成测试 DB engine，用于 TRUNCATE 清理。"""
    eng = create_async_engine(_integration_db_url(), poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


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


@pytest_asyncio.fixture(autouse=True)
async def truncate_tables(
    _integration_engine: AsyncEngine,
) -> AsyncGenerator[None, None]:
    """测试间 TRUNCATE 清理全部 13 表（autouse，setup+teardown 双清）。

    setup 清：前一测试 crash 残留兜底，确保进入测试前干净。
    teardown 清：清理本测试写入行，确保 session 末 _integration_row_count_guard
    断言 13 表为空时不被最后一测的数据误伤（order-dependent false positive）。

    关注点 7（DB bootstrap + TRUNCATE 闭环）：
      - 数据是 app 自身 session（build_runtime 引擎）真 commit 写入的
      - TRUNCATE RESTART IDENTITY CASCADE 清空所有表 + 重置序列
      - autouse 确保集成包内所有测试自动获得干净状态
    """
    tables_sql = ", ".join(f'"{t}"' for t in _GUARD_TABLES)

    # setup：crash 残留兜底
    async with _integration_engine.connect() as conn:
        await conn.execute(
            text(f"TRUNCATE TABLE {tables_sql} RESTART IDENTITY CASCADE")
        )
        await conn.commit()

    yield

    # teardown：清理本测试写入，满足 session 末 guard
    async with _integration_engine.connect() as conn:
        await conn.execute(
            text(f"TRUNCATE TABLE {tables_sql} RESTART IDENTITY CASCADE")
        )
        await conn.commit()


# ---------- Step 3: Real Redis fixture ----------


@pytest_asyncio.fixture
async def integration_redis() -> AsyncGenerator[Redis, None]:
    """真 Redis fixture，db 15，setup/teardown flushdb。

    隔离铁律唯一豁免点：
      本 fixture 是整份集成测试唯一允许 flushdb 的地方。
      flushdb 仅操作 db index {INTEGRATION_REDIS_DB}，不影响其他 db。
    """
    client = Redis.from_url(
        _integration_redis_url(),
        encoding="utf-8",
        decode_responses=True,
    )
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


# ---------- Step 6: Integration RuntimeResources ----------


@pytest_asyncio.fixture
async def integration_runtime(
    integration_redis: Redis,
    _bootstrap_integration_db: None,
) -> AsyncGenerator[Any, None]:
    """用集成 settings 构建真 RuntimeResources。

    关注点 4（注入缝生效时机 vs 缓存）：
      LLM 实例在图节点执行时才调 build_provider_llm 构建，
      build_runtime 不缓存 LLM。因此 set_test_llm 可在 runtime
      构建之后再调用，仍能生效。

    关注点 5（lifespan + 任务句柄）：
      本 fixture 将 RuntimeResources 注入 app.state.resources
      （见下面 app fixture），lifespan 检查到非空后跳过重新构建。
      _chat_tasks / register_chat_task 通过 RuntimeResources
      暴露给测试（integration_runtime 返回值），阶段二测试可通过
      rr.register_chat_task 句柄 await 段一收口。
    """
    from app.core.runtime import build_runtime, teardown_runtime

    s = build_integration_settings()
    rr = await build_runtime(s)
    try:
        yield rr
    finally:
        await teardown_runtime(rr)


# ---------- Step 6: App fixture ----------


@pytest_asyncio.fixture
async def app(
    integration_runtime: Any,
) -> AsyncGenerator[Any, None]:
    """FastAPI app with 真 RuntimeResources + 真 lifespan。

    关注点 5（lifespan 二选一）：
      选择「预注入 app.state.resources」方案。
      main.py lifespan 检测到 resources 非空后存活：
        - 不走 redis_lifespan
        - 不调 build_runtime（跳过重建）
        - 不调 teardown_runtime（teardown 由 integration_runtime fixture 负责）
        - yield 后仍调 _shutdown_wait(rr) 等待 chat bg task
      此方案避免双重构建 + 资源泄漏。

    httpx ASGITransport 自动触发 lifespan。本 fixture 不覆写
    application.router.lifespan_context，让真 lifespan 运行。
    """
    from app.main import create_app

    from app.auth.redis_client import get_redis
    from app.core.db import get_db

    application = create_app()

    # 预注入 runtime —— lifespan 见 resources 非空后跳过 rebuild
    application.state.resources = integration_runtime

    # DB / Redis 依赖覆写指向集成资源
    async def _get_db() -> AsyncGenerator[AsyncSession, None]:
        async with integration_runtime.db_session_factory() as session:
            yield session

    async def _get_redis() -> Redis:
        return integration_runtime.audit_redis

    application.dependency_overrides[get_db] = _get_db
    application.dependency_overrides[get_redis] = _get_redis

    try:
        yield application
    finally:
        application.dependency_overrides.clear()
        # 注意：integration_runtime teardown 由该 fixture 负责，不在本层处理


@pytest_asyncio.fixture
async def api_client(app: Any) -> AsyncGenerator[Any, None]:
    """ASGI HTTP client（与根 conftest 同模式，使用 integration app）。"""
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# ---------- Step 4: In-process arq worker fixture ----------


@pytest_asyncio.fixture
async def arq_worker(integration_runtime: Any) -> AsyncGenerator[Callable[[], int], None]:
    """In-process burst arq worker (关注点 1/2)。

    关注点 1（Redis db 一致性）：
      redis_pool=rr.arq_pool 使用与 app 相同的集成 arq pool，
      enqueue/drain 在同一 db index (15) 上操作。

    关注点 2（worker ctx 指向集成栈）：
      自定义 _on_startup 将集成 RuntimeResources 注入 ctx。
      run_audit 从 ctx["resources"] 取出 rr 后：
        - db_session_factory → 连 littlebox_integration
        - audit_redis → 连 db 15
        - audit_graph → 走 build_provider_llm → 受 _test_llm_overrides 控制

    约束（计划 §4）：
      functions=WORKER_SETTINGS["functions"] 使用字符串路径
      ["app.audit.worker.run_audit"]，禁止写成 [run_audit]。
    """
    from arq import Worker

    from app.audit.worker import WORKER_SETTINGS
    from app.state.audit_signals import AuditSignalsManager

    rr = integration_runtime

    async def _on_startup(ctx: dict[str, Any]) -> None:
        """使用集成 RuntimeResources（不重新 build_runtime）。"""
        ctx["resources"] = rr
        ctx["signals_manager"] = AuditSignalsManager(
            rr.audit_redis,
            ttl=rr.settings.audit_redis_ttl_seconds,
        )

    async def _on_shutdown(ctx: dict[str, Any]) -> None:
        """关闭由 integration_runtime fixture 统一处理。"""
        pass

    worker = Worker(
        functions=WORKER_SETTINGS["functions"],  # 字符串路径：["app.audit.worker.run_audit"]
        redis_pool=rr.arq_pool,
        burst=True,
        on_startup=_on_startup,
        on_shutdown=_on_shutdown,
        on_job_start=WORKER_SETTINGS.get("on_job_start"),
        on_job_end=WORKER_SETTINGS.get("on_job_end"),
        max_tries=1,  # 集成测试不验证重试
        job_timeout=WORKER_SETTINGS["job_timeout"],
    )

    async def drain() -> int:
        """消费队列中所有待处理 job。返回成功处理数。

        使用 async_run 而非 run_check 以避免 FailedJobs 异常上抛。
        arq 的 run_check 在 jobs_failed > 0 时 raise FailedJobs，
        而 RED 阶段预期 job 因名不匹配而失败——exception 会掩盖计数。
        async_run 直接处理所有 job 并更新计数器，不主动 raise。
        """
        await worker.async_run()
        # arq 已自行记录失败日志（function not found）
        return worker.jobs_complete

    try:
        yield drain
    finally:
        await worker.close()
