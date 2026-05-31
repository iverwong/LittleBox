"""Step 15 · 红测：SSE 帧序 — intervention_type 帧应早于首个 delta 帧。

RED 真因：`stream_graph_to_sse()`（sse.py:36-61）仅映射 `delta` 帧，
`intervention_type` 从未发射为 SSE 事件——它只在 DB 层 persist_ai_turn 记录。

两轮协议：
  Round 1：正常流 → 创建 session + turn=1
  → 删除 enqueue_audit 写入的 audit:{sid}=pending
  → set_ready(sid, turn=1, redline_triggered=True)
  Round 2：load_audit_state(turn=2, expected_turn=1) 读到 ready
  → route_by_risk → "redline" → call_redline_llm
  → writer({"intervention_type": "redline"})（graph.py:473）
  → stream_graph_to_sse 不转这个 payload → 客户端收不到 intervention_type 帧

Phase 3 修复步骤：在 stream_graph_to_sse 中增加 intervention_type/finish_reason 映射。
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
    """SSE 帧序红测（两轮，预设 audit ready→redline）。"""

    async def test_intervention_before_first_delta(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        """Round 2 路由到 redline，backend 发射了 intervention_type writer 但不转 SSE。

        此测试验证的是「缺 intervention_type SSE 帧」→ 永远 RED，
        直至 Phase 3 补 sse.py mapping。
        """
        from datetime import datetime, timezone
        from app.state.audit_signals import AuditSignalsManager
        from app.schemas.audit import AuditDimensionScores, AuditOutputSchema

        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm("deepseek", FakeMainLLM())

        try:
            # ---- Round 1 ----
            async with api_client.stream(
                "POST", "/api/v1/me/chat/stream",
                json={"content": "第一轮"}, headers=headers,
            ) as resp:
                events1 = await parse_sse_events(resp)

            sid = next(
                (d["session_id"] for t, d in events1 if t == "session_meta"),
                None,
            )
            assert sid is not None, "Round 1 应产生 session_meta"

            import asyncio
            await asyncio.sleep(2.0)  # 等 throttle lock 过期

            # 用 rr.audit_redis（与 load_audit_state 读同实例）
            await integration_runtime.audit_redis.delete(f"audit:{sid}")
            manager = AuditSignalsManager(
                integration_runtime.audit_redis,
                ttl=integration_runtime.settings.audit_redis_ttl_seconds,
            )
            await manager.set_ready(
                sid, turn=1,
                signals=AuditOutputSchema(
                    dimension_scores=AuditDimensionScores(),
                    crisis_detected=False,
                    redline_triggered=True,
                    redline_detail="帧序测试红线触发",
                    turn_summary="SSE帧序验证测试",
                ),
            )

            # ---- Round 2（应 redline 分支） ----
            async with api_client.stream(
                "POST", "/api/v1/me/chat/stream",
                json={"content": "第二轮"}, headers=headers,
            ) as resp:
                events2 = await parse_sse_events(resp)

            # RED 断言：intervention_type 帧不存在（sse.py 不发射它）
            intervention_events = [
                d for t, d in events2 if t == "intervention_type"
            ]
            assert len(intervention_events) > 0, (
                "RED: intervention_type 帧不存在。\n"
                "路由可达 redline（payload 已由 writer 发射），\n"
                "但 stream_graph_to_sse(sse.py:36) 仅转 delta 帧，\n"
                "intervention_type/finish_reason/usage_metadata 全部丢失。\n"
                "事件类型: " + str({t for t, _ in events2})
            )

        finally:
            clear_test_llm()
