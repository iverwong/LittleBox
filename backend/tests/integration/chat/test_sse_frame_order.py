"""Step 15 · 红测：SSE 帧序 — intervention_type 帧应早于首个 delta 帧。

RED 真因：`stream_graph_to_sse()`（sse.py:36-61）仅映射 `delta` 帧，
`intervention_type` 从未发射为 SSE 事件——它只在 DB 层 persist_ai_turn 记录。

graph 的 call_crisis/redline/guidance_llm 节点虽在 emit delta 之前发出
`writer({"intervention_type": "redline"})`（graph.py:473），但该 payload
被 _run_llm_pipeline 提取为 last_intervention_type 变量后仅用于 DB 写入，
未通过 _put 转发到 SSE queue。

因此本测试永远无法 PASS——不是路由问题，是 intervention_type SSE 协议缺失。
仅锁现状，不改行为。
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


class TestSseFrameOrderRed:
    """SSE 帧序红测。"""

    async def test_intervention_before_first_delta(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        """干预帧应早于 delta，但 backend 不发射 intervention_type SSE 帧。

        路由正常可达 redline，但 `_run_llm_pipeline` 中
        `payload.get("intervention_type")` 只写 DB 不发射 SSE。
        帧序断言永远穿透不过 stream_graph_to_sse。锁现状。
        """
        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm("deepseek", FakeMainLLM())

        try:
            async with api_client.stream(
                "POST", "/api/v1/me/chat/stream",
                json={"content": "测试"}, headers=headers,
            ) as resp:
                events = await parse_sse_events(resp)

            delta_indices = [i for i, (t, _) in enumerate(events) if t == "delta"]
            intervention_indices = [
                i for i, (t, _) in enumerate(events) if t == "intervention_type"
            ]

            assert len(intervention_indices) > 0, (
                "RED: intervention_type 帧不存在 — stream_graph_to_sse 不发射它"
            )
            assert intervention_indices[0] < delta_indices[0], (
                "RED: intervention_type 帧应在首个 delta 之前"
            )
        finally:
            clear_test_llm()
