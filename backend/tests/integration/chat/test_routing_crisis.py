"""Step 11 · 红测：crisis 路由。

预期流程（修复后）：
  1. 第 1 轮发消息 → 流正常结束 → enqueue_audit 入队审查
  2. worker drain → 处理审计 → FakeAuditLLM 输出 crisis_detected/crisis_locked
  3. 第 2 轮发消息 → load_audit_state 读取到 crisis 信号
  4. route_by_risk → crisis 分支 → 发出 crisis intervention_type 帧

当前因入队名 bug：
  - worker 不消费第 1 轮的审查 job → audit 永不就绪
  - 第 2 轮 load_audit_state 返回全默认值 → route 走 main
  - crisis intervention_type 帧不产生 → RED
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


class TestCrisisRoutingRed:
    """crisis 路由红测（两轮）。"""

    async def test_crisis_routing_no_intervention_frame(
        self,
        api_client: Any,
        integration_runtime: Any,
        arq_worker: Any,
    ) -> None:
        """第 2 轮应发 crisis intervention_type 帧但未发 → RED。

        两轮协议：
          - Round 1：触发 enqueue_audit（但名不匹配 → worker 不消费）
          - Round 2：load_audit_state 读不到就绪信号 → route 到 main
        """
        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm("deepseek", FakeMainLLM())
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

            # Drain — 0 jobs（入队名 bug）
            processed = await arq_worker()
            assert processed == 0, (
                f"RED 前提：drain 应返回 0（入队名 bug），实际 {processed}"
            )

            # ---- Round 2 ----
            async with api_client.stream(
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "我觉得很孤独"},
                headers=headers,
            ) as resp:
                events2 = await parse_sse_events(resp)

            # RED 断言：不存在 crisis 干预帧
            # （因 audit 信号从未就绪 → route_by_risk 走 main → 无干预）
            crisis_frames = [
                d for t, d in events2
                if t == "intervention_type"
                and d.get("type") in ("crisis_locked", "crisis_detected")
            ]
            assert len(crisis_frames) > 0, (
                "RED: crisis intervention_type 帧未发出。\n"
                "入队名 bug 导致 audit 永不就绪 → load_audit_state 全默认 → route 走 main → 无危机干预。\n"
                f"实际事件类型：{set(t for t, _ in events2)}"
            )

        finally:
            clear_test_llm()
