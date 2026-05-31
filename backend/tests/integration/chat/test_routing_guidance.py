"""Step 13 · GREEN：guidance 路由。

修复后流程：
  1. 第 1 轮发消息 → 流正常结束 → enqueue_audit 入队审查
  2. worker drain → 处理审计 → FakeAuditLLM 输出 guidance ≠ ""
  3. 第 2 轮发消息 → load_audit_state 读到 guidance 信号
  4. route_by_risk → guidance 分支 → 发出 guided intervention_type 帧

注意：AuditOutputSchema.guidance 默认 ""，load_audit_state 做
result.signals.guidance or None → "" 变 None 不触发 guidance 路由。
测试必须设非空字符串。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.chat.factory import clear_test_llm, set_test_llm

from ._helpers import (
    FakeAuditLLM,
    FakeMainLLM,
    make_audit_tool_call,
    parse_sse_events,
    seed_integration_child,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


class TestGuidanceRoutingRed:
    """RED 存档：入队名 bug 时的原始断言（__test__=False 不被 pytest 收集）。"""
    __test__ = False

    async def test_guidance_routing_no_intervention_frame(
        self,
        api_client: Any,
        integration_runtime: Any,
        arq_worker: Any,
    ) -> None:
        """第 2 轮应发 guided intervention_type 帧但未发 → RED。"""
        pass


class TestGuidanceRoutingGreen:
    """guidance 路由 GREEN（两轮）。"""

    async def test_guidance_routing_sends_intervention_frame(
        self,
        api_client: Any,
        integration_runtime: Any,
        arq_worker: Any,
    ) -> None:
        """第 2 轮应发 guided intervention_type 帧。"""
        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm("deepseek", FakeMainLLM())
        set_test_llm(
            "audit_deepseek",
            FakeAuditLLM(tool_calls=make_audit_tool_call(
                guidance="试试和信任的成年人聊聊",
                turn_summary="同伴冲突",
            )),
        )
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
            assert processed == 1, (
                f"drain 应消费 1 个 audit job，实际 {processed}"
            )

            # 等待 throttle lock 过期
            await asyncio.sleep(2.0)

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
                f"GREEN: 应发出 guided intervention_type 帧。\n"
                f"实际事件类型：{set(t for t, _ in events2)}"
            )

            assert any(t == "end" for t, _ in events2), (
                "guidance 分支流应正常结束"
            )

        finally:
            clear_test_llm()
