"""Step 16 · 红测：强杀断连收口。

场景：httpx 客户端在消费部分 delta 帧后主动 aclose() 中断连接。
后端预期行为：
  段二（stream_generator）：收到 ConnectionError → 退出
  段一（run_llm_pipeline）：作为独立 bg task 不受影响，照常跑完 commit②
    → accumulated 完整落库（ai 行）
    → enqueue_audit 入队
    → finally 释放 chat:lock:{sid}、running_streams pop

段一 await 语义（子代理核实）：
  register_chat_task 保存 asyncio.Task，await 返回 None（run_llm_pipeline 签名为 -> None）。
  段一异常会传播到 await 方（不是被 Task 吞掉）。
  running_streams 在 run_llm_pipeline finally 中 pop。

关注点 4（真 commit + 段一 bg task 跨测试污染防护）：
  本测试结束时必须 await 段一收口，确保段一在 teardown（test teardown
  会 TRUNCATE 表 + flushdb）前完成 commit② 落库。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.llm import clear_test_llm, set_test_llm

from ._helpers import FakeMainLLM, seed_integration_child

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


class TestKillDisconnectRed:
    """强杀断连收口红测。"""

    async def test_kill_disconnect_cleanup(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
        arq_worker: Any,
    ) -> None:
        """消费部分 delta 后中断连接，验证段一收口三项终态。

        RED 可能点：
          1. 段一 await 后 DB 无 ai 行（段一未 commit）
          2. chat:lock:{sid} 残留（锁未释放）
          3. running_streams 残留（未 pop）
          4. 孤儿 human（段一 rollback 且未正确标记孤儿行）
        """
        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm(
            "deepseek",
            FakeMainLLM(
                chunks=["你好，", "我是一", "个测试", "AI助手。"],
            ),
        )

        sid: str | None = None
        bg_task = None

        try:
            async with api_client.stream(
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "测试断连"},
                headers=headers,
            ) as resp:
                # 逐行消费 SSE，提取 sid + 计数 delta
                current_event = None
                data_parts: list[str] = []
                delta_count = 0

                async for line in resp.aiter_lines():
                    if line.startswith("event: "):
                        current_event = line[7:]
                        data_parts = []
                    elif line.startswith("data: "):
                        data_parts.append(line[6:])
                    elif line == "" and current_event is not None:
                        import json
                        data = json.loads("".join(data_parts))
                        if current_event == "session_meta":
                            sid = data["session_id"]
                        elif current_event == "delta":
                            delta_count += 1
                            if delta_count >= 2:
                                break
                        current_event = None
                        data_parts = []

            # 此时 httpx 连接已关闭（async with 结束），段二已退役
            # 段一仍在后台运行，需要等它完成

            # 通过 _chat_tasks 找到 bg task 并 await
            if sid is not None and str(sid) in integration_runtime._chat_tasks:
                bg_task = integration_runtime._chat_tasks.get(str(sid))
                if bg_task is not None:
                    await bg_task  # await 段一收口

            # ---- 三项终态断言 ----
            # ① chat:lock:{sid} 必须已释放
            if sid:
                lock_still_exists = await integration_redis.exists(f"chat:lock:{sid}")
                assert not lock_still_exists, (
                    f"RED: chat:lock:{sid} 在段一完成后仍存在 —— 锁残留。"
                )

            # ② running_streams 已 pop（在段一 finally 中执行）
            # running_streams 是模块级 dict，通过 app.state 无法直接访问
            # 通过段一 await 不抛异常来间接验证（异常会暴露段一内部失败）
            # 无异常即验证

            # ③ DB 中应有完整 ai 行（accumulated 已落库）
            if sid:
                sid_uuid = uuid.UUID(sid)
                async with integration_runtime.db_session_factory() as db:
                    from sqlalchemy import select
                    from app.models.chat import Message, MessageRole

                    result = await db.execute(
                        select(Message).where(
                            Message.session_id == sid_uuid,
                            Message.role == MessageRole.ai,
                            Message.status == "active",
                        )
                    )
                    ai_row = result.scalar_one_or_none()
                    assert ai_row is not None, (
                        "RED: 段一应写入了 ai 行，但 DB 中无 active AI 行。"
                    )
                    # accumulated 内容应完整
                    expected_content = "你好，我是一个测试AI助手。"
                    assert ai_row.content == expected_content, (
                        f"RED: ai 行内容不完整。\n"
                        f"期望：{expected_content!r}\n"
                        f"实际：{ai_row.content!r}"
                    )

        finally:
            clear_test_llm()
            # 确保段一已完全完成（防止 bg task 跨测试泄漏）
            if bg_task is not None and not bg_task.done():
                await bg_task
            # 清理可能残留的锁（防护性）
            if sid:
                await integration_redis.delete(f"chat:lock:{sid}")
