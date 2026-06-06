"""RuntimeResources.register_chat_task 单元测试（M9-patch1 Step 2）。

覆盖三场景：
1. task 正常完成 → done_callback 自动 pop
2. task 抛异常 → 日志留痕（含 sid 上下文）
3. 多实例 _chat_tasks dict 隔离（default_factory 生效）
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
pytestmark = pytest.mark.asyncio(loop_scope="function")

from app.core.runtime import RuntimeResources


@pytest.fixture
def runtime_resources() -> RuntimeResources:
    """真实 RuntimeResources 实例（依赖字段用 mock 填充）。"""
    return RuntimeResources(
        settings=MagicMock(),
        db_engine=AsyncMock(),
        db_session_factory=MagicMock(),
        audit_redis=AsyncMock(),
        arq_pool=AsyncMock(),
        main_graph=MagicMock(),
        audit_graph=MagicMock(),
    )


async def test_register_chat_task_auto_pops_on_done(
    runtime_resources: RuntimeResources,
) -> None:
    """
    Given a registered chat task,
    When the task finishes normally,
    Then it should be auto-removed from _chat_tasks via done_callback.
    """
    sid = "test-sid-1"

    async def _quick() -> None:
        pass

    task = asyncio.create_task(_quick())
    runtime_resources.register_chat_task(sid, task)
    assert sid in runtime_resources._chat_tasks

    await task
    # done_callback 经 loop.call_soon 调度，不在同一 tick 同步执行
    await asyncio.sleep(0)

    assert sid not in runtime_resources._chat_tasks


async def test_register_chat_task_logs_unhandled_exception(
    runtime_resources: RuntimeResources,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Given a chat task that raises,
    When the task completes,
    Then the unhandled exception should be logged with sid context.
    """
    sid = "test-sid-2"
    caplog.set_level(logging.ERROR)

    async def _broken() -> None:
        raise RuntimeError("boom")

    task = asyncio.create_task(_broken())
    runtime_resources.register_chat_task(sid, task)

    # 用 gather(return_exceptions=True) 避免异常被重新抛出
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    assert sid not in runtime_resources._chat_tasks
    assert any(
        r.name == "app.core.runtime"
        and getattr(r, "sid", None) == sid
        and "chat task crashed unhandled" in r.message
        for r in caplog.records
    )


async def test_chat_tasks_default_factory_isolated() -> None:
    """
    Given two RuntimeResources instances,
    When each registers a task,
    Then their _chat_tasks dicts should be independent (no shared mutable).
    """
    rr1 = RuntimeResources(
        settings=MagicMock(),
        db_engine=AsyncMock(),
        db_session_factory=MagicMock(),
        audit_redis=AsyncMock(),
        arq_pool=AsyncMock(),
        main_graph=MagicMock(),
        audit_graph=MagicMock(),
    )
    rr2 = RuntimeResources(
        settings=MagicMock(),
        db_engine=AsyncMock(),
        db_session_factory=MagicMock(),
        audit_redis=AsyncMock(),
        arq_pool=AsyncMock(),
        main_graph=MagicMock(),
        audit_graph=MagicMock(),
    )

    assert rr1._chat_tasks is not rr2._chat_tasks  # 不同实例

    async def _dummy() -> None:
        pass

    t1 = asyncio.create_task(_dummy())
    t2 = asyncio.create_task(_dummy())
    rr1.register_chat_task("a", t1)
    rr2.register_chat_task("b", t2)

    assert len(rr1._chat_tasks) == 1
    assert len(rr2._chat_tasks) == 1
    assert "a" in rr1._chat_tasks
    assert "b" in rr2._chat_tasks

    await asyncio.gather(t1, t2, return_exceptions=True)
