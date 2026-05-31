"""Step 13 · 红测：guidance 路由。

预期流程（修复后）：
  1. 第 1 轮发消息 → 流正常结束 → enqueue_audit 入队审查
  2. worker drain → 处理审计 → FakeAuditLLM 输出 guidance ≠ None
  3. 第 2 轮发消息 → load_audit_state 读到 guidance 信号
  4. route_by_risk → guidance 分支 → 发出 guided intervention_type 帧

当前因入队名 bug：
  - 与 Steps 11–12 同一根因 → RED
"""

from __future__ import annotations

from typing import Any

import pytest

from app.chat.factory import clear_test_llm, set_test_llm

from ._helpers import FakeMainLLM, parse_sse_events, seed_integration_child

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


class TestGuidanceRoutingRed:
    """guidance 路由红测（两轮）。"""

    async def test_guidance_routing_no_intervention_frame(
        self,
        api_client: Any,
        integration_runtime: Any,
        arq_worker: Any,
    ) -> None:
        """第 2 轮应发 guided intervention_type 帧但未发 → RED。"""
        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm("deepseek", FakeMainLLM())
        try:
            # ---- Round 1 ----
            async with api_client.stream(
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "我最近和同学吵架了"},
                headers=headers,
            ) as resp:
                await parse_sse_events(resp)

            import asyncio
            await asyncio.sleep(0.05)

            processed = await arq_worker()
            assert processed == 0, (
                f"RED 前提：drain 应返回 0（入队名 bug），实际 {processed}"
            )

            # ---- Round 2 ----
            async with api_client.stream(
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "我应该怎么处理"},
                headers=headers,
            ) as resp:
                events2 = await parse_sse_events(resp)

            guidance_frames = [
                d for t, d in events2
                if t == "intervention_type"
                and d.get("type") == "guided"
            ]
            assert len(guidance_frames) > 0, (
                "RED: guided intervention_type 帧未发出。\n"
                "入队名 bug → audit 不就绪 → route 走 main → 无引导干预。\n"
                f"实际事件类型：{set(t for t, _ in events2)}"
            )

        finally:
            clear_test_llm()
