"""Step 14 · 红测：多轮 turn_number round-trip。

预期流程（修复后）：
  1. 第 N 轮发消息 → 流结束 → enqueue_audit（turn_number=N）
  2. worker drain → run_audit 处理 → AuditSignalsManager.set_ready(turn=N)
  3. 第 N+1 轮发消息 → load_audit_state 读 turn N 的信号
  4. turn_number 在第 N+1 轮的 graph 上下文中正确传递

当前因入队名 bug：
  - 第 N 轮的审查 job 不被 worker 消费 → 无 ready 信号写入
  - 第 N+1 轮 load_audit_state 读到空 → 无法验证 turn_number round-trip
  - RED
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.chat.factory import clear_test_llm, set_test_llm

from ._helpers import FakeMainLLM, parse_sse_events, seed_integration_child

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


class TestMultiroundTurnRed:
    """多轮 turn_number round-trip 红测。"""

    async def test_turn_number_round_trip(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
        arq_worker: Any,
    ) -> None:
        """验证 turn_number 在多轮间的 round-trip。

        RED 断言：第 1 轮后 audit 不就绪 → 第 2 轮 load_audit_state 读不到
        上一轮的审查结果 → 验证不了 turn_number 穿透。
        """
        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm("deepseek", FakeMainLLM())
        try:
            # ---- Round 1（turn 1） ----
            async with api_client.stream(
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "第一轮消息"},
                headers=headers,
            ) as resp:
                events1 = await parse_sse_events(resp)

            # 从 session_meta 中提取 sid
            for t, d in events1:
                if t == "session_meta":
                    sid = d["session_id"]
                    break
            assert sid is not None, "session_meta 应有 sid"

            import asyncio
            await asyncio.sleep(0.05)

            # Drain worker — 应消费 1 个 job，但入队名 bug 阻止匹配
            processed = await arq_worker()
            assert processed == 1, (
                f"RED: drain 应消费 1 个 audit job，实际 {processed}。\n"
                "入队名 bug → worker 不匹配 'run_audit' → audit 永不 ready。"
            )

            # 检查 Redis 中 audit:{sid} key（enqueue_audit 已写 pending）
            audit_key = f"audit:{sid}"
            audit_raw = await integration_runtime.audit_redis.get(audit_key)

            # ---- Round 2（turn 2） ----
            async with api_client.stream(
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "第二轮消息"},
                headers=headers,
            ) as resp:
                events2 = await parse_sse_events(resp)

            # RED 断言：round 2 的流中应无任何路由干预帧
            # 因为 audit 从没就绪过 → load_audit_state 返回全默认 → route 走 main
            intervention_frames = [
                d for t, d in events2
                if t == "intervention_type"
            ]
            assert len(intervention_frames) == 0, (
                "RED: 应无干预帧（audit 不来信号）。\n"
                f"实际 {len(intervention_frames)} 个干预帧：{intervention_frames}"
            )

            # 辅助验证：audit:{sid} key 存在（enqueue_audit 已写入 pending）
            assert audit_raw is not None, (
                "辅助检查：audit:{} 不存在 —— enqueue_audit 可能未调用".format(sid)
            )
            # payload 中的 status 应为 pending（worker 没消费 job）
            payload = json.loads(audit_raw)
            assert payload.get("status") == "pending", (
                f"RED 辅助检查：status 应为 'pending'，实际 {payload.get('status')!r}。\n"
                "worker 不消费 job → set_ready/set_failed 不被调用 → 保持 pending。"
            )

        finally:
            clear_test_llm()
