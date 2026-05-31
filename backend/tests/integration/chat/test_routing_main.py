"""Step 10 · 红测：main 路由全链路。

预期流程（修复后）：
  1. child 发消息 → POST /api/v1/me/chat/stream
  2. 流正常结束（SSE delta + end 帧）
  3. commit② 中 enqueue_audit 发送审查 job → drain 消费 1 个
  4. route_by_risk 走 main（自然结束无干预信号）

当前因入队名 bug（Step 9）：
  - enqueue_audit 用短名 "run_audit" → arq worker 注册全路径 → job 不被消费
  - drain 返回 0（RED：应为 1）
  - 流能正常结束（main 路由是默认分支）
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


class TestMainRoutingRed:
    """main 路由全链路红测。"""

    async def test_main_routing_stream_completes(
        self,
        api_client: Any,
        integration_runtime: Any,
    ) -> None:
        """流正常结束：assert delta + end 帧存在。

        即使入队名 bug 阻止 audit 就绪，main 路由作为默认分支
        仍应正常产字。
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

    async def test_main_routing_drain_zero(
        self,
        api_client: Any,
        integration_runtime: Any,
        arq_worker: Any,
    ) -> None:
        """Drain 应返回 1 但返回 0 → RED（入队名 bug）。

        enqueue_audit 在 commit② 中被调用（段一 finally 之前），
        流结束时机在 commit② + enqueue 之后，因此 drain 时 job 已入队。
        但因入队名不匹配，worker 不消费。
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
                await parse_sse_events(resp)

            # 段一 bg task 应在流结束后很快完成（已在 finally 发 None 哨兵）
            # 少量 yield 给 event loop 处理 bg task 的收尾
            import asyncio
            await asyncio.sleep(0.05)

            processed = await arq_worker()
            # RED 断言：入队名 bug 导致 worker 不匹配 job
            assert processed == 1, (
                f"RED: drain 返回 {processed}（期望 1）。\n"
                "入队名 bug：enqueue 用 'run_audit'，worker 注册 'app.audit.worker.run_audit'。\n"
                "worker 不会消费此 job → audit 永不就绪 → 全链路中断。"
            )
        finally:
            clear_test_llm()
