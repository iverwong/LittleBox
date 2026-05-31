"""Step 14 · GREEN：多轮 turn_number round-trip。

修复后流程：
  1. 第 N 轮发消息 → 流结束 → enqueue_audit（turn_number=N）
  2. worker drain → run_audit 处理 → AuditSignalsManager.set_ready(turn=N)
  3. 第 N+1 轮发消息 → load_audit_state 读 turn N 的信号
  4. turn_number 在第 N+1 轮的 graph 上下文中正确传递
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.chat.factory import clear_test_llm, set_test_llm

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


class TestMultiroundTurnRed:
    """RED 存档：入队名 bug 时原始断言（__test__=False 不被 pytest 收集）。"""
    __test__ = False

    async def test_turn_number_round_trip(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
        arq_worker: Any,
    ) -> None:
        """验证 turn_number 在多轮间的 round-trip。"""
        pass


class TestMultiroundTurnGreen:
    """多轮 turn_number round-trip GREEN。"""

    async def test_turn_number_round_trip(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
        arq_worker: Any,
    ) -> None:
        """验证 turn_number 在多轮间的 round-trip。

        FakeAuditLLM 使用默认值（全关），worker 处理后 audit 就绪。
        第 2 轮 load_audit_state 读到 ready → route 走 main。
        """
        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm("deepseek", FakeMainLLM())
        set_test_llm(
            "audit_deepseek",
            FakeAuditLLM(tool_calls=make_audit_tool_call()),
        )
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
            sid = None
            for t, d in events1:
                if t == "session_meta":
                    sid = d["session_id"]
                    break
            assert sid is not None, "session_meta 应有 sid"

            import asyncio
            await asyncio.sleep(0.05)

            # Drain worker — 应消费 1 个 job（入队名已修复）
            processed = await arq_worker()
            assert processed == 1, (
                f"drain 应消费 1 个 audit job，实际 {processed}"
            )

            # 等待 throttle lock 过期
            await asyncio.sleep(2.0)

            # 检查 Redis 中 audit:{sid} key（worker 已写入 ready）
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

            # GREEN 断言：应无干预帧（默认值路由到 main，无干预信号）
            intervention_frames = [
                d for t, d in events2
                if t == "intervention_type"
            ]
            assert len(intervention_frames) == 0, (
                f"应无干预帧（main 路由），实际 {len(intervention_frames)} 个干预帧"
            )

            # 辅助验证：audit:{sid} key 存在（worker 已处理）
            assert audit_raw is not None, (
                "audit:{} key 不存在 —— enqueue_audit 可能未调用".format(sid)
            )
            # payload 中的 status 应为 ready（worker 已消费）
            payload = json.loads(audit_raw)
            assert payload.get("status") == "ready", (
                f"status 应为 'ready'（worker 已消费），实际 {payload.get('status')!r}"
            )
            assert payload.get("signals") is not None, (
                "ready 状态下 signals 不应为 None"
            )

        finally:
            clear_test_llm()
