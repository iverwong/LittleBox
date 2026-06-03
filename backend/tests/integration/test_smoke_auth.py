"""Step 22 · GREEN 基线：auth/children 贯通真基建。

一条薄 smoke 走真 PG + 真 app，确认基建可用，不重复 CRUD 覆盖。

验证点：
  - POST /api/v1/auth/login（确认 auth 端点响应，不依赖预置数据）
  - GET /api/v1/me（确认认证链路贯通）
  - GET /api/v1/me/profile（确认 profile 端点可用）

预期 GREEN：基建贯通测试。
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


class TestAuthSmokeGreen:
    """auth/children 基建冒烟（GREEN 预期）。"""

    async def test_health_endpoint(
        self,
        api_client: Any,
    ) -> None:
        """健康检查端点在集成上下文中可响应。"""
        resp = await api_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok", (
            f"health endpoint 返回异常：{data}"
        )

    async def test_login_endpoint_reachable(
        self,
        api_client: Any,
    ) -> None:
        """login 端点可达（预期 422 因缺 body，即路由注册正确）。"""
        resp = await api_client.post("/api/v1/auth/login")
        # 422（验证错误）说明路由正确注册；401（未认证）说明中间件拦截
        # 不依赖具体状态码，只确认不 404
        assert resp.status_code != 404, (
            "/api/v1/auth/login 路由未注册或不可用"
        )

    async def test_me_endpoint_without_auth(
        self,
        api_client: Any,
    ) -> None:
        """未认证时 /api/v1/me 返回 401。"""
        resp = await api_client.get("/api/v1/me")
        assert resp.status_code == 401, (
            f"未认证请求应返回 401，实际 {resp.status_code}"
        )

    async def test_me_profile_endpoint_without_auth(
        self,
        api_client: Any,
    ) -> None:
        """未认证时 /api/v1/me/profile 返回 401。"""
        resp = await api_client.get("/api/v1/me/profile")
        assert resp.status_code == 401, (
            f"未认证请求应返回 401，实际 {resp.status_code}"
        )

    async def test_db_and_redis_available(
        self,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        """集成库 DB 和 Redis 均可操作。"""
        # DB 可查询
        async with integration_runtime.db_session_factory() as db:
            from sqlalchemy import text
            result = await db.execute(text("SELECT 1"))
            assert result.scalar_one() == 1

        # Redis 可 SET/GET
        await integration_redis.set("smoke:test", "ok")
        val = await integration_redis.get("smoke:test")
        assert val == "ok"
