"""Step 10 · GREEN：main 路由全链路。

修复后流程：
  1. child 发消息 → POST /api/v1/me/chat/stream
  2. 流正常结束（SSE delta + end 帧）
  3. commit② 中 enqueue_audit 送审查 job → drain 消费 1 个
  4. route_by_risk 走 main（默认值无干预信号）
"""

from __future__ import annotations

from typing import Any

import pytest
from app.core.llm import clear_test_llm, set_test_llm

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


class TestMainRoutingRed:
    """RED 存档：入队名 bug 时的原始断言（__test__=False 不被 pytest 收集）。"""
    __test__ = False

    async def test_main_routing_drain_zero(
        self,
        api_client: Any,
        integration_runtime: Any,
        arq_worker: Any,
    ) -> None:
        """Drain 应返回 1 但返回 0 → RED（入队名 bug）。"""
        pass


class TestMainRoutingGreen:
    """main 路由全链路 GREEN。"""

    async def test_main_routing_stream_completes(
        self,
        api_client: Any,
        integration_runtime: Any,
    ) -> None:
        """流正常结束：assert delta + end 帧存在。

        main 路由作为默认分支，即使审计信号不就绪仍应正常产字。
        """
        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm("deepseek", FakeMainLLM())
        try:
            async with api_client.stream(
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "你好"},
                headers=headers,
            ) as resp:
                events = await parse_sse_events(resp)

            # 应有 delta 帧（LLM 产字）
            assert any(t == "delta" for t, _ in events), "应有 delta 帧"
            # 应有 end 帧（自然结束）
            assert any(t == "end" for t, _ in events), "应有 end 帧"

        finally:
            clear_test_llm()

    async def test_main_routing_drain_consumes_job(
        self,
        api_client: Any,
        integration_runtime: Any,
        arq_worker: Any,
    ) -> None:
        """Drain 应消费 1 个 audit job（入队名修复后）。

        enqueue_audit 在 commit② 中被调用（段一 finally 之前），
        流结束时机在 commit② + enqueue 之后，因此 drain 时 job 已入队。
        加上 FakeAuditLLM（默认全关），worker 处理审计后返回 1。
        """
        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm("deepseek", FakeMainLLM())
        set_test_llm("audit_deepseek", FakeAuditLLM(
            tool_calls=make_audit_tool_call(),
        ))
        try:
            async with api_client.stream(
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "你好"},
                headers=headers,
            ) as resp:
                await parse_sse_events(resp)

            # 段一 bg task 应在流结束后很快完成（已在 finally 发 None 哨兵）
            # 少量 yield 给 event loop 处理 bg task 的收尾
            import asyncio
            await asyncio.sleep(0.05)

            processed = await arq_worker()
            assert processed == 1, (
                f"GREEN: drain 应消费 1 个 job，实际 {processed}。"
            )

        finally:
            clear_test_llm()
