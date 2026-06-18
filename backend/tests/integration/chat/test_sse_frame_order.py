"""Step 15 · GREEN：SSE 帧序 — intervention_type 帧应早于首个 delta 帧。

移除 redline 路由后，帧序仅验证 crisis 介入场景。
"""
from __future__ import annotations

from typing import Any

import pytest
from app.core.llm import clear_test_llm, set_test_llm
from app.core.llm_topology import Role

from ._helpers import FakeMainLLM, parse_sse_events, seed_integration_child

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


class TestSseFrameOrderGreen:
    """SSE 帧序 GREEN（两轮，手动设 audit ready→crisis）。"""

    async def test_intervention_before_first_delta(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        """Round 2 路由到 crisis，intervention_type 帧应在首个 delta 前出现。"""
        from app.domain.audit.schemas import AuditDimensionScores, AuditOutputSchema
        from app.domain.audit.signals import AuditSignalsManager

        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm(Role.MAIN, FakeMainLLM(["干预回复"]))

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

            # 设 audit ready → crisis
            await integration_runtime.audit_redis.delete(f"audit:{sid}")
            manager = AuditSignalsManager(
                integration_runtime.audit_redis,
                ttl=integration_runtime.settings.audit_redis_ttl_seconds,
            )
            await manager.set_ready(
                sid, turn=1,
                signals=AuditOutputSchema(
                    dimension_scores=AuditDimensionScores(),
                    crisis_detected=True,
                    crisis_topic="危机主题",
                    turn_summary="SSE 帧序验证测试",
                ),
            )

            # ---- Round 2（应 crisis 分支） ----
            async with api_client.stream(
                "POST", "/api/v1/me/chat/stream",
                json={"content": "第二轮"}, headers=headers,
            ) as resp:
                events2 = await parse_sse_events(resp)

            # GREEN 断言：intervention_type 帧存在
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
