"""Step 15 · 红测：SSE 帧序 — intervention_type 帧严格早于首个 delta 帧。

`load_audit_state` 在 turn==1 时直接返回 all-False 不读 Redis（graph.py:174）。
因此需要两轮协议：第 1 轮产生 turn=1 的 ai 行，第 2 轮 load_audit_state 读取 turn 1 的 ready 信号。

本测试在 Round 1 结束后将 audit ready 信号写入 Redis（绕过 arq worker），
使 Round 2 的 load_audit_state 读到 redline 信号 → 路由到 redline 分支。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.chat.factory import clear_test_llm, set_test_llm

from ._helpers import FakeMainLLM, parse_sse_events, seed_integration_child

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


class TestSseFrameOrderRed:
    """SSE 帧序红测（两轮）。"""

    @pytest.mark.xfail(strict=True, reason=(
        "Phase 2: load_audit_state poll_wait 30s 阻塞段一，手动预设的 ready 信号"
        "被 enqueue_audit 的 pending 覆写或因 Redis 客户端不一致不可见。"
        "Phase 3 修入队名 bug 后 audit 正常完成 → 两轮协议可达 redline 分支。"
    ))
    async def test_intervention_before_first_delta(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
        arq_worker: Any,
    ) -> None:
        """Round 2 中 intervention_type 帧应早于首个 delta 帧。

        使用 redline 分支（非 crisis）避免 target_message_id 依赖。
        """
        from datetime import datetime, timezone
        from app.state.audit_signals import AuditSignalsManager
        from app.schemas.audit import AuditDimensionScores, AuditOutputSchema

        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm("deepseek", FakeMainLLM())

        sid: str | None = None
        try:
            # ---- Round 1 ----
            async with api_client.stream(
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "第一轮"},
                headers=headers,
            ) as resp:
                events1 = await parse_sse_events(resp)

            for t, d in events1:
                if t == "session_meta":
                    sid = d["session_id"]
                    break
            assert sid is not None, "Round 1 应产生 session_meta"

            import asyncio
            await asyncio.sleep(2.0)

            # 删除 Round 1 enqueue_audit 写入的 audit:{sid}=pending
            # 再设为 ready（使用 redline 分支，不依赖 rolling_summaries 行）
            # 使用 rr.audit_redis（与 load_audit_state 读的同实例）
            await integration_runtime.audit_redis.delete(f"audit:{sid}")
            manager = AuditSignalsManager(
                integration_runtime.audit_redis,
                ttl=integration_runtime.settings.audit_redis_ttl_seconds,
            )
            await manager.set_ready(
                sid,
                turn=1,
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
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "第二轮"},
                headers=headers,
            ) as resp:
                events2 = await parse_sse_events(resp)

            delta_indices = [
                i for i, (t, _) in enumerate(events2)
                if t == "delta"
            ]
            intervention_indices = [
                i for i, (t, _) in enumerate(events2)
                if t == "intervention_type"
            ]

            # RED 断言 ①：存在 intervention_type 帧
            assert len(intervention_indices) > 0, (
                "RED: 无 intervention_type 帧。\n"
                f"事件类型：{set(t for t, _ in events2)}"
            )

            # RED 断言 ②：首个 intervention_type 帧严格早于首个 delta 帧
            assert intervention_indices[0] < delta_indices[0], (
                f"RED: intervention_type 帧在第 {intervention_indices[0]} 位，\n"
                f"首个 delta 在第 {delta_indices[0]} 位 —— 帧序错误。\n"
                "intervention 帧应在首 token 前发出。"
            )

        finally:
            clear_test_llm()
