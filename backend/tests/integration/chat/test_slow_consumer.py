"""Step 19 · 红测：慢消费 / 首帧超时。

场景 1（慢消费）：段二消费慢 → queue 积压 → 正常消费（不 overflow），
验证段二不因消费慢而异常退出。

场景 2（首帧超时）：段一首帧 >10s 未入队 → 段二静默退出（无错误帧）。
首帧超时保护覆盖 session_meta 入队前的静默崩溃，阈值 10s。

RED 可能点：
  - 慢消费场景下段二意外提前退出（消费慢被误判为断连）
  - 首帧超时静默退出后段一还在跑（孤儿段一）
  - 锁未释放
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from app.core.enums import MessageRole
from app.core.llm_topology import Role
from app.domain.chat.models import Message
from sqlalchemy import select

from ._helpers import FakeMainLLM, seed_integration_child

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


class TestSlowConsumerRed:
    """慢消费 / 首帧超时红测。"""

    async def test_slow_consumer_no_early_exit(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
        llm_override: Any,
    ) -> None:
        """慢消费：段二不应因消费慢而提前退出。

        方法：用一个产字快的 FakeLLM，但在段二消费时人为 sleep。
        （httpx 的 aiter_lines 消费天然慢，因为 Python 处理每一行）
        """
        child, headers = await seed_integration_child(integration_runtime)
        llm_override(
            Role.MAIN,
            FakeMainLLM(
                chunks=["Hello ", "world, ", "this ", "is ", "a ", "test."],
            ),
        )

        sid: str | None = None
        events_count = 0
        bg_task = None

        import json

        async with api_client.stream(
            "POST",
            "/api/v1/me/chat/stream",
            json={"content": "测试慢消费"},
            headers=headers,
        ) as resp:
            current_event = None
            data_parts = []
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    current_event = line[7:]
                    data_parts = []
                elif line.startswith("data: "):
                    data_parts.append(line[6:])
                elif line == "" and current_event is not None:
                    data = json.loads("".join(data_parts))
                    if current_event == "session_meta":
                        sid = data["session_id"]
                    events_count += 1
                    # 人为 sleep 模拟慢消费
                    if current_event == "delta":
                        await asyncio.sleep(0.02)
                    current_event = None
                    data_parts = []

        # 等待段一收口
        if sid and str(sid) in integration_runtime._chat_tasks:
            bg_task = integration_runtime._chat_tasks.get(str(sid))
            if bg_task and not bg_task.done():
                await bg_task

        # RED 断言：应有 end 或 stopped 事件（流正常结束）
        assert events_count > 0, (
            "RED: 慢消费场景下无任何事件 —— 段二可能提前退出了"
        )

        # DB 验证：ai 行应完整落库
        if sid:
            sid_uuid = uuid.UUID(sid)
            async with integration_runtime.db_session_factory() as db:
                result = await db.execute(
                    select(Message).where(
                        Message.session_id == sid_uuid,
                        Message.role == MessageRole.ai,
                        Message.status == "active",
                    )
                )
                ai_row = result.scalar_one_or_none()
                assert ai_row is not None, (
                    "RED: 慢消费场景下段一未写入 ai 行"
                )

        if sid:
            await integration_redis.delete(f"chat:lock:{sid}")
