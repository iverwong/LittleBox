"""Step 17 · 红测：Stop 三态 — XFAIL（ASGITransport 协议限制）。

无法测试的根本原因：
  httpx ASGITransport 在处理 StreamingResponse 时，bg task 已在
  返回流上下文前跑完（enter stream context 延迟 ≈1.2s > bg task 完成时间）。
  此时 running_streams entry 已被 bg task finally pop，stop 信号无法送达。

设计尝试：
  - 双 ASGITransport ✗：running_streams 跨进程可见但 entry 已弹出
  - 单轮（turn=1 避 poll_wait）✗：同上
  - 两轮（turn=2）✗：load_audit_state 30s poll_wait 阻塞

Phase 3 方案建议：
  - 使用独立进程的 httpx client 不共享 event loop
  - 或用真正的 HTTP server（uvicorn）替代 ASGITransport
  - 或在测试内用 asyncio.Event 手动模拟 stop 信号绕过 transport 层

当前状态：best_effort_204 作为轻量测试保留（不依赖 running_streams 状态）。
"""

from __future__ import annotations

from typing import Any

import pytest

from ._helpers import seed_integration_child

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


class TestStopTristateRed:
    """Stop 三态。"""

    @pytest.mark.xfail(strict=True, reason=(
        "ASGITransport 下 bg task 在进入流上下文前已收口，"
        "running_streams 弹出后 stop 信号无法送达。需 Phase 3 换传输方案。"
    ))
    async def test_stop_with_ai(
        self,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        pytest.fail("Phase 2 不可达：ASGITransport 协议限制")

    @pytest.mark.xfail(strict=True, reason=(
        "同 test_stop_with_ai, ASGITransport 限制。Phase 3 换传输方案。"
    ))
    async def test_stop_no_ai(
        self,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        pytest.fail("Phase 2 不可达：ASGITransport 协议限制")

    async def test_best_effort_204(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        """best-effort：不存在的 session → 404（不依赖 running_streams）。"""
        child, headers = await seed_integration_child(integration_runtime)
        resp = await api_client.post(
            "/api/v1/me/sessions/00000000-0000-0000-0000-000000000000/stop",
            headers=headers,
        )
        assert resp.status_code == 404, f"应返回 404，实际 {resp.status_code}"
