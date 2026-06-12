"""Step 12 · GREEN：redline 路由。

修复后流程：
  1. 第 1 轮发消息 → 流正常结束 → enqueue_audit 入队审查
  2. worker drain → 处理审计 → FakeAuditLLM 输出 redline_triggered
  3. 第 2 轮发消息 → load_audit_state 读到 redline 信号
  4. route_by_risk → redline 分支 → 发出 redline intervention_type 帧
"""

from __future__ import annotations

from typing import Any

import pytest
from app.core.llm import clear_test_llm, set_test_llm
from app.core.llm_topology import Role

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


class TestRedlineRoutingRed:
    """RED 存档：入队名 bug 时的原始断言（__test__=False 不被 pytest 收集）。"""
    __test__ = False

    async def test_redline_routing_no_intervention_frame(
        self,
        api_client: Any,
        integration_runtime: Any,
        arq_worker: Any,
    ) -> None:
        """第 2 轮应发 redline intervention_type 帧但未发 → RED。"""
        pass


class TestRedlineRoutingGreen:
    """redline 路由 GREEN（两轮）。"""

    async def test_redline_routing_sends_intervention_frame(
        self,
        api_client: Any,
        integration_runtime: Any,
        arq_worker: Any,
    ) -> None:
        """第 2 轮应发 redline intervention_type 帧。"""
        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm(Role.MAIN, FakeMainLLM())
        set_test_llm(
            Role.AUDIT,
            FakeAuditLLM(tool_calls=make_audit_tool_call(
                redline_triggered=True,
                redline_detail="explicit content",
            )),
        )
        try:
            # ---- Round 1 ----
            async with api_client.stream(
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "我想伤害自己"},
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
                json={"content": "我活着没有意义"},
                headers=headers,
            ) as resp:
                events2 = await parse_sse_events(resp)

            redline_frames = [
                d for t, d in events2
                if t == "intervention_type"
                and d.get("type") == "redline"
            ]
            assert len(redline_frames) > 0, (
                f"GREEN: 应发出 redline intervention_type 帧。\n"
                f"实际事件类型：{set(t for t, _ in events2)}"
            )

            assert any(t == "end" for t, _ in events2), (
                "redline 分支流应正常结束"
            )

        finally:
            clear_test_llm()
