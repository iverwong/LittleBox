"""真实 DB 故障场景下,handler 装饰器的反应。

异常映射依据见 backend/scripts/probe_sa_asyncpg_exceptions.py
(2026-06-24 跑出的真实故障 → 异常类型映射,7 场景全部跟 spec §4.3 表一致)。

| 场景                      | 抛                                |
|---------------------------|-----------------------------------|
| 杀 backend(conn_a 被 conn_b 杀)| sqlalchemy.exc.InterfaceError   |
| statement_timeout         | sqlalchemy.exc.DBAPIError (基类)  |
| use_closed_connection     | sqlalchemy.exc.ResourceClosedError|
| sql syntax error          | sqlalchemy.exc.ProgrammingError   |

Catch 列表:{DBAPIError, ResourceClosedError}
- DBAPIError 覆盖 InterfaceError(子类)+ DBAPIError 基类
- ResourceClosedError 在另一棵树上,单点 catch
- ProgrammingError 显式 re-raise,不被兜住(bug 走 stack trace 暴露)
"""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from app.domain.expert.context_schema import ExpertContextSchema
from app.domain.expert.schemas import DailyDimensionSummary
from app.domain.expert.tools import EXPERT_TOOL_HANDLERS
from langchain_core.messages import ToolMessage
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

# 测试 ``_with_db_error_handling`` 装饰器的 catch 行为,所有 case 都必须走
# 装饰后的 handler(EXPERT_TOOL_HANDLERS["SearchHistoryInput"])而非裸 _search_history
# ——直接调 _search_history 时异常不会被 catch,会原样上抛。
_search_history = EXPERT_TOOL_HANDLERS["SearchHistoryInput"]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


def _make_ctx(child_user_id, db_session_factory, report_date):
    """构造测试 ExpertContextSchema,frozen dataclass 只能完整构造。

    测试场景下 ctx 不是业务真实 ctx,只需关键字段;其他字段用 None 占位,
    因为 _search_history 实际只用 child_user_id / owned_session_ids / report_date /
    db_session_factory。
    """
    return ExpertContextSchema(
        child_user_id=child_user_id,
        owned_session_ids=frozenset(),
        report_date=report_date,
        db_session_factory=db_session_factory,
        session_id=child_user_id,  # 占位,_search_history 不读
        dimension_summary=DailyDimensionSummary(peak=0.0, mean=0.0, high_ratio=0.0),
        crisis_detected_today=False,
        max_output_attempts=1,
        token_budget=100_000,
        child_profile=None,
        settings=None,
        shared_http_client=None,
    )


class _Runtime:
    """测试用 fake Runtime[ExpertContextSchema]。"""

    def __init__(self, ctx):
        self.context = ctx


# Case 1: 杀 backend → InterfaceError → catch
async def test_search_history_kill_backend_returns_error_tool_message(
    caplog,
    db_session_factory_killer,
):
    """conn_a 拿 pid,conn_b 杀它,conn_a 再查 → 真实 InterfaceError → 装饰器兜住。

    关键点:handler 必须**复用**被杀的那条 session,而不是开新 session。
    集成 RuntimeResources 的 db_engine 带 ``pool_pre_ping=True``,
    新 session 会拿到 fresh conn,绕开被杀 conn,触发不了故障。
    所以这里把 ``victim_cm`` 持有,杀掉它的底层 conn 后,
    factory 仍 yield 这条 victim(其 conn 已死)给 handler 用。
    """
    killer_factory, victim_factory = db_session_factory_killer

    @asynccontextmanager
    async def wrapped_victim():
        async with victim_factory() as sess:
            yield sess

    import datetime as _dt

    victim_cm = wrapped_victim()
    victim = await victim_cm.__aenter__()
    try:
        pid = (await victim.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        async with killer_factory() as killer:
            await killer.execute(text(f"SELECT pg_terminate_backend({pid})"))
        await asyncio.sleep(0.2)

        # yield 同一条 victim(底层 conn 已死),不走 pool_pre_ping 拿 fresh conn
        @asynccontextmanager
        async def dead_victim_factory():
            yield victim

        runtime = _Runtime(
            _make_ctx(
                child_user_id="00000000-0000-0000-0000-000000000001",
                db_session_factory=dead_victim_factory,
                report_date=_dt.date(2026, 6, 24),
            )
        )
        with caplog.at_level("ERROR", logger="expert.tools"):
            msg = await _search_history(
                args={"keywords": ["游戏"], "source": "turn_summary"},
                runtime=runtime,
                tool_call_id="tid-kb",
            )
    finally:
        await victim_cm.__aexit__(None, None, None)

    assert isinstance(msg, ToolMessage)
    assert msg.tool_call_id == "tid-kb"
    payload = json.loads(msg.content)
    assert "数据库" in payload["error"]
    assert any(r.exc_info for r in caplog.records if "db_error" in r.message)


# Case 2: statement_timeout → DBAPIError(基类)→ catch
async def test_search_history_statement_timeout_returns_error_tool_message(
    caplog,
    db_session_factory,
):
    """LOCK TABLE + statement_timeout=1ms → 真实 DBAPIError → 装饰器兜住。

    关键点:handler 的 SELECT 在空表上几 ms 内完成,纯靠 statement_timeout
    抓不到(timeout 必须 > 查询耗时)。所以从**另一个** session 持有
    ``LOCK TABLE rolling_summaries IN ACCESS EXCLUSIVE MODE``,handler SELECT
    被阻塞,然后 1ms timeout 即触发 ``QueryCanceledError`` → DBAPIError。
    """
    import datetime as _dt

    # Lock holder:独立 session 持锁,handler SELECT 必须等。
    # 锁放在 handler 上下文**外**显式 acquire,handler 进来时锁已生效;
    # finally 里 close session 让 SA 自动 rollback 事务、释放锁。
    lock_holder = db_session_factory()
    await lock_holder.execute(text("LOCK TABLE rolling_summaries IN ACCESS EXCLUSIVE MODE"))
    try:

        @asynccontextmanager
        async def timed_out_factory():
            async with db_session_factory() as db:
                await db.execute(text("SET statement_timeout = 1"))  # 1ms
                yield db

        runtime = _Runtime(
            _make_ctx(
                child_user_id="00000000-0000-0000-0000-000000000001",
                db_session_factory=timed_out_factory,
                report_date=_dt.date(2026, 6, 24),
            )
        )
        with caplog.at_level("ERROR", logger="expert.tools"):
            msg = await _search_history(
                args={"keywords": ["游戏"], "source": "turn_summary"},
                runtime=runtime,
                tool_call_id="tid-st",
            )
    finally:
        await lock_holder.close()  # rollback transaction → release lock

    assert isinstance(msg, ToolMessage)
    payload = json.loads(msg.content)
    assert "数据库" in payload["error"]


# Case 3: use closed connection → ResourceClosedError → catch
async def test_search_history_use_closed_connection_returns_error_tool_message(
    caplog,
    db_session_factory,
):
    """关底层 Connection 后再 reuse → ResourceClosedError。

    ResourceClosedError 不在 DBAPIError 树下(在 SQLAlchemyError 另一支),
    验证必须双 catch 列表都覆盖。

    为什么不能只关 Session(``async with Session() as sess: pass``):
    SA 2.0.22+ 默认 ``close_resets_only=True``,``Session.close()`` 只把状态
    重置为 ``CLOSE_IS_RESET``,不丢底层 conn;reuse 时 session 仍可正常
    执行,不抛 ResourceClosedError(实测 2026-06-25 探针确认)。
    必须 ``await conn.close()`` 显式关底层 Connection —— Connection 才是
    close 后 execute 必抛的(参见 ``scripts/probe_sa_asyncpg_exceptions.py``
    case_07 用 ``engine.connect()`` 直接拿 conn 演示 ResourceClosedError)。
    """
    import datetime as _dt

    # 不走 async with —— async with 退出时会调 sess.close() 把 _close_state
    # 从 ACTIVE 推到 CLOSED,reuse 时抛 InvalidRequestError 而非
    # ResourceClosedError,绕开我们要测的 catch 路径。
    # 直接拿 session,只关底层 conn,保留 ACTIVE 状态。
    sess = db_session_factory()
    await sess.execute(text("SELECT 1"))  # 强制 checkout
    conn = await sess.connection()
    await conn.close()  # 关底层 conn,_close_state 仍为 ACTIVE
    try:

        @asynccontextmanager
        async def closed_factory():
            yield sess  # session whose underlying conn is closed

        runtime = _Runtime(
            _make_ctx(
                child_user_id="00000000-0000-0000-0000-000000000001",
                db_session_factory=closed_factory,
                report_date=_dt.date(2026, 6, 24),
            )
        )
        with caplog.at_level("ERROR", logger="expert.tools"):
            msg = await _search_history(
                args={"keywords": ["游戏"], "source": "turn_summary"},
                runtime=runtime,
                tool_call_id="tid-cl",
            )
    finally:
        await sess.close()

    assert isinstance(msg, ToolMessage)
    payload = json.loads(msg.content)
    assert "数据库" in payload["error"]


# Case 4: SQL 语法错 → ProgrammingError → re-raise(不被 catch)
async def test_search_history_programming_error_propagates(db_session_factory):
    """SELEKT 1(故意拼错)→ 真实 ProgrammingError → 装饰器 re-raise,不被兜住。

    except ProgrammingError: raise 必须放在 DBAPIError 之前,否则会被宽 catch 兜住
    变成 bug mask,验证该 re-raise 顺序生效。

    通过 monkey-patch ``tools.search_turn_summaries`` 直接抛 ProgrammingError,
    确保 ``_search_history`` 内的 ``except ProgrammingError: raise`` 路径被走到。
    """
    import datetime as _dt

    @asynccontextmanager
    async def broken_factory():
        async with db_session_factory() as db:
            yield db

    from app.domain.expert import tools as tools_module

    original_func = tools_module.search_turn_summaries

    async def boom(*args, **kwargs):
        raise ProgrammingError("simulated SQL syntax error", params={}, orig=Exception("SELEKT"))

    tools_module.search_turn_summaries = boom
    try:
        runtime = _Runtime(
            _make_ctx(
                child_user_id="00000000-0000-0000-0000-000000000001",
                db_session_factory=broken_factory,
                report_date=_dt.date(2026, 6, 24),
            )
        )
        with pytest.raises(ProgrammingError):
            await _search_history(
                args={"keywords": ["游戏"], "source": "turn_summary"},
                runtime=runtime,
                tool_call_id="tid-pe",
            )
    finally:
        tools_module.search_turn_summaries = original_func
