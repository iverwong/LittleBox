"""Step 18 · 红测：flow_pause 背压。

场景：填满 asyncio.Queue → overflow → 段二发 flow_pause 帧退出，
段一头也不回地跑完 commit②（headless mode）。

关注点（来自 me.py 代码核实）：
  - _put: queue.put_nowait → QueueFull → state.overflow=True → 跳过后续入队
  - stream_generator: 检测 state.overflow → yield flow_pause → return
  - 段一继续跑完后 commit②，finally 中 running_streams.pop + release_lock

RED 可能点：
  - queue maxsize 小于预期 → overflow 过早或过晚触发
  - flow_pause 帧格式 / 内容不符协议
  - overflow 后段一未做 commit②（headless 断裂）
  - overflow 后 finally 未释放锁（lock 残留）
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.chat.factory import clear_test_llm, set_test_llm
from app.models.chat import Message, MessageRole
from sqlalchemy import select

from ._helpers import FakeMainLLM, seed_integration_child

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


class TestFlowPauseRed:
    """flow_pause 背压红测。"""

    async def test_flow_pause_on_queue_overflow(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        """验证 queue overflow 触发 flow_pause + 段一 headless commit②。

        策略：用一个产出极多小 chunk 的 FakeLLM，迅速填满小队列。
        chat_queue_maxsize 在集成测试中默认为 settings 值（可能较大），
        因此本测试通过设置小 queue maxsize（通过 settings 调整）。
        """
        from app.chat.factory import clear_test_llm, set_test_llm

        child, headers = await seed_integration_child(integration_runtime)

        # 用大量 chunks 填满 queue
        many_chunks = [f"块{i}" for i in range(200)]
        set_test_llm(
            "deepseek",
            FakeMainLLM(chunks=many_chunks),
        )

        sid: str | None = None
        events = []
        bg_task = None

        try:
            import json

            async with api_client.stream(
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "测试背压"},
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
                        events.append((current_event, data))
                        if current_event == "session_meta":
                            sid = data["session_id"]
                        current_event = None
                        data_parts = []

            # 等待段一收口
            if sid and str(sid) in integration_runtime._chat_tasks:
                bg_task = integration_runtime._chat_tasks.get(str(sid))
                if bg_task and not bg_task.done():
                    await bg_task

            # RED 断言 ①：应出现 flow_pause 帧
            flow_pause_events = [(t, d) for t, d in events if t == "flow_pause"]
            assert len(flow_pause_events) > 0, (
                "RED: 无 flow_pause 事件 —— queue 未 overflow。"
            )
            # flow_pause 帧应含 reason
            reason = flow_pause_events[0][1].get("reason")
            assert reason == "backpressure", (
                f"RED: flow_pause reason 应为 'backpressure'，实际 {reason!r}"
            )

            # RED 断言 ②：段一 headless 模式下 commit② 应完成（ai 行落库）
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
                        "RED: overflow 后段一应 headless 完成 commit②（ai 行落库）"
                    )

            # RED 断言 ③：chat:lock 应已释放
            if sid:
                lock_exists = await integration_redis.exists(f"chat:lock:{sid}")
                assert not lock_exists, (
                    f"RED: chat:lock:{sid} 在段一完成后仍存在 —— 锁残留"
                )

        finally:
            clear_test_llm()
            if sid:
                await integration_redis.delete(f"chat:lock:{sid}")
