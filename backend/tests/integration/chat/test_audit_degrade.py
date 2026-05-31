"""Step 21 · GREEN 基线：audit 失败降级现状。

验证点（计划 §21）：
  - worker 不就绪 / audit 超时 → load_audit_state 返回全默认
  → 信号全 False + guidance=None → route_by_risk 走 main
  → 断言现状，不修改行为。

本测试预期 GREEN，因为它断言的是当前正确行为（降级路径正常）。
如果此测试 RED，说明降级路径本身已损坏，须报阻塞。

入队名 bug 背景下：
  - enqueue_audit 入队 job → worker 不消费（名不匹配）
  - audit 永远不会就绪
  - 降级路径（全默认 → main）是当前系统的兜底行为
  - 本测试验证这个兜底行为正常工作
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


class TestAuditDegradeGreen:
    """audit 失败降级基线（GREEN 预期）。"""

    async def test_audit_degrade_to_main(
        self,
        api_client: Any,
        integration_runtime: Any,
        arq_worker: Any,
    ) -> None:
        """audit 不就绪 → load_audit_state 返回默认 → 走 main 分支。

        GREEN 断言：
          1. 流正常结束（main 路由作为降级兜底正常产字）
          2. 无干预帧（audit 就绪时才产生干预）
          3. drain 可能 0（入队名 bug 的外部表现，但降级路径自身正确）

        本测试不关心 drain 结果，只验证降级路径能兜住。
        """
        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm("deepseek", FakeMainLLM())
        try:
            async with api_client.stream(
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "测试降级"},
                headers=headers,
            ) as resp:
                events = await parse_sse_events(resp)

            # GREEN 断言 ①：有 delta 帧（main 分支正常产字）
            assert any(t == "delta" for t, _ in events), (
                "降级路径异常：无 delta 帧。main 分支未正常产字。"
            )

            # GREEN 断言 ②：流正常结束（有 end 或 stopped 事件）
            assert any(t in ("end", "stopped") for t, _ in events), (
                "降级路径异常：流未正常结束。"
            )

            # GREEN 断言 ③：无干预帧（audit 不就绪时不应有干预）
            intervention_frames = [
                (t, d) for t, d in events if t == "intervention_type"
            ]
            assert len(intervention_frames) == 0, (
                f"降级路径异常：出现 {len(intervention_frames)} 个干预帧。\n"
                "audit 不就绪时不应产生干预信号。"
            )

        finally:
            clear_test_llm()

    async def test_audit_degrade_two_rounds(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
        arq_worker: Any,
    ) -> None:
        """两轮降级验证：第 2 轮 load_audit_state 读不到就绪信号 → 仍走 main。

        GREEN：
          第 2 轮路由仍为 main（无干预帧），降级路径在连续轮次中稳定。
        """
        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm("deepseek", FakeMainLLM())
        try:
            # ---- Round 1 ----
            async with api_client.stream(
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "第一轮"},
                headers=headers,
            ) as resp:
                await parse_sse_events(resp)

            # 等待 throttle lock（1.5s TTL）过期，否则 Round 2 被 429
            import asyncio
            await asyncio.sleep(2.0)

            # ---- Round 2 ----
            async with api_client.stream(
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "第二轮"},
                headers=headers,
            ) as resp:
                events2 = await parse_sse_events(resp)

            # GREEN：第 2 轮仍走 main（无干预帧）
            intervention = [
                d for t, d in events2 if t == "intervention_type"
            ]
            assert len(intervention) == 0, (
                f"第 2 轮降级路径异常：出现干预帧 {intervention}"
            )

            # GREEN：有 delta 帧
            assert any(t == "delta" for t, _ in events2), (
                "第 2 轮降级路径异常：无 delta 帧"
            )

        finally:
            clear_test_llm()
