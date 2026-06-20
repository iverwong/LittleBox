"""RuntimeResources 生命周期回归测试（T14）。

覆盖：frozen 不可变、graphs compiled 接口、teardown 关闭顺序。
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock, call

import httpx
import pytest
from app.core.runtime import RuntimeResources, teardown_runtime


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
        assert rr.shared_http_client is not None
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
        shared_http_client=MagicMock(),
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
        shared_http_client=MagicMock(),
        main_graph=MagicMock(),
        audit_graph=MagicMock(),
    )
    # main_graph 使用 astream（chat 流式），audit_graph 使用 ainvoke（单次调用）
    assert hasattr(rr.main_graph, "astream"), "main_graph 缺少 astream 接口"
    assert hasattr(rr.audit_graph, "ainvoke"), "audit_graph 缺少 ainvoke 接口"


@pytest.mark.asyncio
async def test_teardown_runtime_order():
    """teardown_runtime 按 shared_http_client → arq_pool → audit_redis → db_engine 顺序关闭。

    全序列等长比对（C4），验证 arq_close 入参含 close_connection_pool=True。
    shared_http_client 必须先关：进程退出前的所有 LLM 流可能仍在用
    池里的 keep-alive 连接,httpx aclose 会 drain 完挂起请求再退出。
    """
    parent = MagicMock()
    rr = MagicMock()
    # RuntimeResources 含 TYPE_CHECKING 字段（ArqRedis），不能直接用 spec 构造
    rr.shared_http_client = MagicMock()
    rr.shared_http_client.aclose = AsyncMock(side_effect=parent.http_aclose)
    rr.arq_pool = MagicMock()
    rr.arq_pool.aclose = AsyncMock(side_effect=parent.arq_aclose)
    rr.audit_redis = MagicMock()
    rr.audit_redis.aclose = AsyncMock(side_effect=parent.audit_aclose)
    rr.db_engine = MagicMock()
    rr.db_engine.dispose = AsyncMock(side_effect=parent.db_dispose)

    await teardown_runtime(rr)

    assert parent.mock_calls == [
        call.http_aclose(),
        call.arq_aclose(close_connection_pool=True),
        call.audit_aclose(),
        call.db_dispose(),
    ]


@pytest.mark.asyncio
async def test_shared_http_client_has_limits():
    """shared_http_client 必须带 httpx.Limits + httpx.Timeout 配置,避免每轮现造丢 keep-alive。"""
    from app.main import app, lifespan

    async with lifespan(app):
        rr = app.state.resources
        assert isinstance(rr.shared_http_client, httpx.AsyncClient)
        # 取 limits 实际配置(走 transport._pool 内部字段)
        pool = rr.shared_http_client._transport._pool  # type: ignore[attr-defined]
        assert pool._max_keepalive_connections == 20
        assert pool._max_connections == 100
        assert pool._keepalive_expiry == 30.0
        # timeout 各段就位
        timeout = rr.shared_http_client.timeout
        assert timeout.connect == 10.0
        assert timeout.read == 60.0  # LLM_REQUEST_TIMEOUT_SECONDS
        assert timeout.write == 10.0
        assert timeout.pool == 10.0
