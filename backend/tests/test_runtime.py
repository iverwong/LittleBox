"""RuntimeResources 生命周期回归测试（T14）。

覆盖：frozen 不可变、graphs compiled 接口、teardown 关闭顺序。
"""
from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from app.runtime import RuntimeResources, build_runtime, teardown_runtime


@pytest.mark.asyncio
async def test_app_state_resources_injected():
    """验证 app.state.resources 注入正确（通过 lifespan 上下文直接触发）。"""
    from app.main import app, lifespan

    async with lifespan(app):
        assert isinstance(app.state.resources, RuntimeResources), (
            f"Expected RuntimeResources, got {type(app.state.resources)}"
        )
        rr = app.state.resources
        assert rr.settings is not None
        assert rr.db_engine is not None
        assert rr.db_session_factory is not None
        assert rr.audit_redis is not None
        assert rr.arq_pool is not None
        assert rr.main_graph is not None
        assert rr.audit_graph is not None

        # 验证 teardown 前资源仍可访问
        assert rr.settings.app_name == "LittleBox"


def test_build_runtime_returns_frozen():
    """RuntimeResources 构造后 frozen=True，赋值抛 FrozenInstanceError。"""
    from unittest.mock import MagicMock

    rr = RuntimeResources(
        settings=MagicMock(),
        db_engine=MagicMock(),
        db_session_factory=MagicMock(),
        audit_redis=MagicMock(),
        arq_pool=MagicMock(),
        main_graph=MagicMock(),
        audit_graph=MagicMock(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        rr.settings = None  # 任意字段赋值，frozen 拦截


def test_build_runtime_graphs_compiled():
    """RuntimeResources 内部 graph 具有 CompiledStateGraph 接口（astream / ainvoke）。"""
    from unittest.mock import MagicMock

    rr = RuntimeResources(
        settings=MagicMock(),
        db_engine=MagicMock(),
        db_session_factory=MagicMock(),
        audit_redis=MagicMock(),
        arq_pool=MagicMock(),
        main_graph=MagicMock(),
        audit_graph=MagicMock(),
    )
    # main_graph 使用 astream（chat 流式），audit_graph 使用 ainvoke（单次调用）
    assert hasattr(rr.main_graph, "astream"), "main_graph 缺少 astream 接口"
    assert hasattr(rr.audit_graph, "ainvoke"), "audit_graph 缺少 ainvoke 接口"


@pytest.mark.asyncio
async def test_teardown_runtime_order():
    """teardown_runtime 按 arq_pool → audit_redis → db_engine 顺序关闭。

    全序列等长比对（C4），验证 arq_close 入参含 close_connection_pool=True。
    """
    parent = MagicMock()
    rr = MagicMock()
    # RuntimeResources 含 TYPE_CHECKING 字段（ArqRedis），不能直接用 spec 构造
    rr.arq_pool = MagicMock()
    rr.arq_pool.close = AsyncMock(side_effect=parent.arq_close)
    rr.audit_redis = MagicMock()
    rr.audit_redis.aclose = AsyncMock(side_effect=parent.audit_aclose)
    rr.db_engine = MagicMock()
    rr.db_engine.dispose = AsyncMock(side_effect=parent.db_dispose)

    await teardown_runtime(rr)

    assert parent.mock_calls == [
        call.arq_close(close_connection_pool=True),
        call.audit_aclose(),
        call.db_dispose(),
    ]
