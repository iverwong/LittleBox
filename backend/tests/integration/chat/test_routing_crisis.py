"""Step 11 · GREEN：crisis 路由。

修复后流程：
  1. 第 1 轮发消息 → 流正常结束 → enqueue_audit 入队审查
  2. worker drain → 处理审计 → FakeAuditLLM 输出 crisis_detected
  3. 第 2 轮发消息 → load_audit_state 读到 crisis 信号
  4. route_by_risk → crisis 分支 → 发出 crisis intervention_type 帧
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


class TestCrisisRoutingRed:
    """RED 存档：入队名 bug 时的原始断言（__test__=False 不被 pytest 收集）。"""
    __test__ = False

    async def test_crisis_routing_no_intervention_frame(
        self,
        api_client: Any,
        integration_runtime: Any,
        arq_worker: Any,
    ) -> None:
        """第 2 轮应发 crisis intervention_type 帧但未发 → RED。"""
        pass


class TestCrisisRoutingGreen:
    """crisis 路由 GREEN（两轮）。"""

    async def test_crisis_routing_sends_intervention_frame(
        self,
        api_client: Any,
        integration_runtime: Any,
        arq_worker: Any,
    ) -> None:
        """第 2 轮应发 crisis intervention_type 帧。

        两轮协议：
          - Round 1：触发 enqueue_audit → worker drain → FakeAuditLLM 输出 crisis
          - Round 2：load_audit_state 读到 crisis → route_by_risk → crisis 分支
        """
        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm("deepseek", FakeMainLLM())
        set_test_llm(
            "audit_deepseek",
            FakeAuditLLM(tool_calls=make_audit_tool_call(
                crisis_detected=True,
                crisis_topic="self-harm risk",
            )),
        )
        try:
            # ---- Round 1 ----
            async with api_client.stream(
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "我今天很难过"},
                headers=headers,
            ) as resp:
                await parse_sse_events(resp)

            import asyncio
            await asyncio.sleep(0.05)

            # Drain — 入队名修复后应消费 1 个 job
            processed = await arq_worker()
            assert processed == 1, (
                f"drain 应消费 1 个 audit job，实际 {processed}"
            )

            # 等待 throttle lock（1.5s TTL）过期，否则 Round 2 被 429
            await asyncio.sleep(2.0)

            # ---- Round 2 ----
            async with api_client.stream(
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "我觉得很孤独"},
                headers=headers,
            ) as resp:
                events2 = await parse_sse_events(resp)

            # GREEN 断言：存在 crisis 干预帧
            crisis_frames = [
                d for t, d in events2
                if t == "intervention_type"
                and d.get("type") == "crisis"
            ]
            assert len(crisis_frames) > 0, (
                f"GREEN: 应发出 crisis intervention_type 帧。\n"
                f"实际事件类型：{set(t for t, _ in events2)}"
            )

            # GREEN 断言：流正常结束
            assert any(t == "end" for t, _ in events2), (
                "crisis 分支流应正常结束（有 end 帧）"
            )

        finally:
            clear_test_llm()
