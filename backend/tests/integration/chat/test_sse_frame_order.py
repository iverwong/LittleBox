"""Step 15 · GREEN：SSE 帧序 — intervention_type 帧应早于首个 delta 帧。

修复后：
  stream_graph_to_sse 仍然只映射 delta 帧，
  但 me.py run_llm_pipeline 在解析到 payload.intervention_type 时
  直接 _put(frame_sse_event("intervention_type", {"type": it_raw})),
  该 SSE 事件在首个 delta 帧之前被写入队列 → 帧序正确。

两轮协议：
  Round 1：正常流 → 创建 session + turn=1
  → 删除 enqueue_audit 写入的 audit:{sid}=pending
  → set_ready(sid, turn=1, redline_triggered=True)
  Round 2：load_audit_state(turn=2, expected_turn=1) 读到 ready
  → route_by_risk → "redline" → call_redline_llm
  → writer({"intervention_type": "redline"})（graph.py:473）
  → me.py 收到 payload → _put SSE event → 再后面的 delta 帧
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
    """RED 存档：SSE 帧序红测（__test__=False 不被 pytest 收集）。"""
    __test__ = False

    async def test_intervention_before_first_delta(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        """Round 2 路由到 redline，intervention_type 帧不存在。"""
        pass


class TestSseFrameOrderGreen:
    """SSE 帧序 GREEN（两轮，手动设 audit ready→redline）。"""

    async def test_intervention_before_first_delta(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        """Round 2 路由到 redline，intervention_type 帧应在首个 delta 前出现。

        手动管理 audit 信号（不依赖 arq worker），独立验证 RC2 修复。
        同时用 FakeMainLLM 覆盖 audit_deepseek provider，
        防止 call_redline_llm 调用真实 API。
        """
        from datetime import datetime, timezone
        from app.domain.audit.signals import AuditSignalsManager
        from app.domain.audit.schemas import AuditDimensionScores, AuditOutputSchema

        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm("deepseek", FakeMainLLM())
        # 覆盖 audit_deepseek（call_redline_llm 使用此 key）防真 API 调用
        set_test_llm("audit_deepseek", FakeMainLLM(["干预回复"]))

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
                    turn_summary="SSE 帧序验证测试",
                ),
            )

            # ---- Round 2（应 redline 分支） ----
            async with api_client.stream(
                "POST", "/api/v1/me/chat/stream",
                json={"content": "第二轮"}, headers=headers,
            ) as resp:
                events2 = await parse_sse_events(resp)

            # GREEN 断言：intervention_type 帧存在（RC2 修复后）
            intervention_events = [
                d for t, d in events2 if t == "intervention_type"
            ]
            assert len(intervention_events) > 0, (
                f"GREEN: intervention_type 帧应存在。\n"
                f"事件类型: {set(t for t, _ in events2)}"
            )

            # 帧序断言：intervention_type 的首个索引 < delta 的首个索引
            event_types = [t for t, _ in events2]
            first_intervention = event_types.index("intervention_type")
            first_delta = event_types.index("delta") if "delta" in event_types else len(event_types)
            assert first_intervention < first_delta, (
                f"GREEN: intervention_type 索引 {first_intervention}"
                f" 应早于首个 delta 索引 {first_delta}"
            )

        finally:
            clear_test_llm()
