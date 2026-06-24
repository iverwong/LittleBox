"""一次性 calibration probe：实测各 DB 故障场景下 SA/asyncpg 抛的异常类型。

不进入测试套件，跑完贴结果，作为 handler 异常 catch 列表的依据。
7 个场景逐个打印 outer type + MRO + root cause + message，详见 spec §4.3 的映射表。

跑法：docker compose exec -T api python -m scripts.probe_sa_asyncpg_exceptions
（spec 里写的 `backend.scripts.*` 路径是 git repo 视角；容器内 cwd 是 /app，
`backend/` 没有作为 Python package 暴露在 sys.path 上，实际进的是 `scripts.*`。）
"""

import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.environ["LB_DATABASE_URL"]


async def probe(name: str, coro) -> None:
    """执行协程并按统一格式打印外层异常信息（type / MRO / root_cause / message）。"""
    try:
        await coro()
        print(f"[{name}] NO EXCEPTION (unexpected)")
    except Exception as e:
        sa_type = type(e).__name__
        sa_module = type(e).__module__
        sa_mro = " -> ".join(c.__name__ for c in type(e).__mro__ if c.__name__ != "object")
        root_cause = e.__cause__ or e.__context__
        root_name = (
            f"{type(root_cause).__module__}.{type(root_cause).__name__}" if root_cause else None
        )
        print(f"[{name}] outer={sa_module}.{sa_type}")
        print(f"  mro: {sa_mro}")
        print(f"  root_cause: {root_name}")
        print(f"  message: {str(e)[:100]}")


async def main() -> None:
    engine = create_async_engine(DATABASE_URL)

    # 01 normal_select: 正常查 SELECT 1,期望无异常
    async def case_01():
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    await probe("01_normal_select", case_01)

    # 02 sql_syntax_error: 故意拼错 "SELEKT",期望 sqlalchemy.exc.ProgrammingError
    async def case_02():
        async with engine.connect() as conn:
            await conn.execute(text("SELEKT 1"))

    await probe("02_sql_syntax_error", case_02)

    # 03 table_not_exist: 查不存在的表,期望 sqlalchemy.exc.ProgrammingError
    async def case_03():
        async with engine.connect() as conn:
            await conn.execute(text("SELECT * FROM nonexistent_xyz"))

    await probe("03_table_not_exist", case_03)

    # 04 kill_other_backend: conn_a 拿 pid,conn_b 杀,conn_a 再查。
    # 用 sleep 给 asyncpg 一拍时间检测到对端关闭,避免自杀 race。
    async def case_04():
        async with engine.connect() as conn_a:
            pid = (await conn_a.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            async with engine.connect() as conn_b:
                await conn_b.execute(text(f"SELECT pg_terminate_backend({pid})"))
            await asyncio.sleep(0.2)
            await conn_a.execute(text("SELECT 1"))

    await probe("04_kill_other_backend", case_04)

    # 05 statement_timeout: SET statement_timeout=100ms,然后 SELECT pg_sleep(1) 超时。
    # 期望 sqlalchemy.exc.DBAPIError(基类,不落到 OperationalError 子类)。
    async def case_05():
        async with engine.connect() as conn:
            await conn.execute(text("SET statement_timeout=100"))
            await conn.execute(text("SELECT pg_sleep(1)"))

    await probe("05_statement_timeout", case_05)

    # 06 unique_violation: 临时表加 PK,二次 INSERT 同 PK,期望 sqlalchemy.exc.IntegrityError。
    # TEMP TABLE 走每会话的临时命名空间,不影响真实库表。
    async def case_06():
        async with engine.connect() as conn:
            await conn.execute(
                text("CREATE TEMP TABLE probe_unique_violation (id INT PRIMARY KEY)")
            )
            await conn.execute(text("INSERT INTO probe_unique_violation (id) VALUES (1)"))
            await conn.execute(text("INSERT INTO probe_unique_violation (id) VALUES (1)"))
            await conn.execute(text("DROP TABLE probe_unique_violation"))

    await probe("06_unique_violation", case_06)

    # 07 use_closed_connection: async with 块退出后会话已 close,再 .execute 应抛
    # sqlalchemy.exc.ResourceClosedError(在 SQLAlchemyError 另一支,不挂在 DBAPIError 下)。
    async def case_07():
        closed_sess = await engine.connect()
        await closed_sess.close()
        await closed_sess.execute(text("SELECT 1"))

    await probe("07_use_closed_connection", case_07)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
