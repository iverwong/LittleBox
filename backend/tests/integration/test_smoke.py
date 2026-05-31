"""集成测试基建冒烟测试（M9.5 Step 2–6）。

确认真 DB / 真 Redis / 真 RuntimeResources 可用，不重复 CRUD 覆盖。
"""
from __future__ import annotations

import pytest


pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


class TestInfrastructureSmoke:
    """基建冒烟：DB / Redis / RuntimeResources 贯通。"""

    async def test_db_bootstrap_and_truncate(
        self,
        _bootstrap_integration_db: None,
        truncate_tables: None,
    ) -> None:
        """DB 可 bootstrap + TRUNCATE 正常执行（无异常即通过）。"""
        pass

    async def test_redis_flushdb(
        self,
        integration_redis,
    ) -> None:
        """Redis fixture SET/GET 跨测试隔离。"""
        await integration_redis.set("smoke_key", "smoke_val")
        val = await integration_redis.get("smoke_key")
        assert val == "smoke_val"

    async def test_runtime_resources(
        self,
        integration_runtime,
    ) -> None:
        """RuntimeResources 含真 engine / session_factory / graphs。"""
        rr = integration_runtime
        # db_session_factory 可创建 session
        async with rr.db_session_factory() as session:
            from sqlalchemy import text
            result = await session.execute(text("SELECT 1 AS val"))
            assert result.scalar_one() == 1
        # main_graph 可编译
        assert rr.main_graph is not None
        assert rr.audit_graph is not None
        # register_chat_task 句柄可暴露
        assert hasattr(rr, "register_chat_task")
        assert hasattr(rr, "_chat_tasks")

    async def test_app_accepts_request(
        self,
        api_client,
    ) -> None:
        """App fixture 可接受 HTTP 请求（health check）。"""
        resp = await api_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok"
