"""Step 20 · 红测：compression 链路。

压缩触发条件（代码核实）：
  CONTEXT_COMPRESS_THRESHOLD_TOKENS = 500_000（compression.py:19）
  commit② 中 usage_meta["input_tokens"] + usage_meta["output_tokens"] >= 阈值时
  设置 session.needs_compression = True
  下一轮 user 消息到达时（段一上段阻塞压缩检查）触发压缩流程。

场景：
  1. 第 1 轮：FakeMainLLM 报告 usage_metadata >= 阈值 → 翻 needs_compression
  2. 第 2 轮：段一检测到 needs_compression → 发 compression_start
     → 调 compression LLM 做摘要 → 写 summary 行 + 旧消息 compressed 标记
     → 重建 initial_state["messages"] → 发 compression_end → 正常出字

注意：compression LLM 使用 provider key "compression_deepseek"（build_provider_llm 调），
因此也需要 set_test_llm("compression_deepseek", fake_compression_llm)。

RED 可能点：
  - needs_compression 在 commit② 中被设置但下一轮未触发压缩
  - compression_start / compression_end 帧未发出
  - 旧消息状态未变更为 compressed
  - summary 行未写入
  - 压缩后 initial_state["messages"] 重建错误 → 后续 LLM 调用异常
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.chat.factory import clear_test_llm, set_test_llm
from app.models.chat import Message, MessageRole, MessageStatus
from sqlalchemy import select

from ._helpers import FakeMainLLM, parse_sse_events, seed_integration_child

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


class _FakeCompressionLLM:
    """极简 compression FakeLLM：返回一段摘要文本。

    compression 流程的 build_provider_llm("compression_deepseek", settings)
    调 ainvoke(c_input) 返回 AIMessage，extract_compression_summary 解析 content。
    """

    async def ainvoke(self, messages, **kwargs):
        from langchain_core.messages import AIMessage
        return AIMessage(content="用户表达了情绪困扰，AI 提供了安慰和建议。")

    def with_retry(self, **kwargs):
        return self

    def with_fallbacks(self, fallbacks, **kwargs):
        return self


class TestCompressionRed:
    """compression 链路红测。"""

    async def test_compression_round_trip(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        """两轮压缩链路：翻标志 → 触发压缩 → 帧 + DB 验证。

        使用超大 usage_metadata 确保 needs_compression 被翻正。
        """
        child, headers = await seed_integration_child(integration_runtime)
        # 第 1 轮 LLM 报告超大 usage → 翻 needs_compression
        set_test_llm(
            "deepseek",
            FakeMainLLM(
                chunks=["第一轮回复"],
                usage_metadata={
                    "input_tokens": 600_000,
                    "output_tokens": 100_000,
                    "total_tokens": 700_000,
                },
            ),
        )
        set_test_llm("compression_deepseek", _FakeCompressionLLM())

        sid: str | None = None

        try:
            # ---- Round 1 ----
            async with api_client.stream(
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "第一轮消息"},
                headers=headers,
            ) as resp:
                events1 = await parse_sse_events(resp)

            # 从 session_meta 获取 sid
            for t, d in events1:
                if t == "session_meta":
                    sid = d["session_id"]
                    break
            assert sid is not None, "应有 session_meta 帧"

            import asyncio

            # 等待 throttle lock（1.5s TTL）过期，否则 Round 2 会被 429 拦截
            await asyncio.sleep(2.0)

            # ---- Round 2 应触发 compression ----
            set_test_llm(
                "deepseek",
                FakeMainLLM(
                    chunks=["第二轮回复，这是压缩后的新对话。"],
                ),
            )

            async with api_client.stream(
                "POST",
                "/api/v1/me/chat/stream",
                json={"content": "第二轮消息"},
                headers=headers,
            ) as resp:
                events2 = await parse_sse_events(resp)

            # 等待段一收口
            if str(sid) in integration_runtime._chat_tasks:
                task = integration_runtime._chat_tasks.get(str(sid))
                if task and not task.done():
                    await task

            # RED 断言 ①：第二轮应有 compression_start 帧
            has_compression_start = any(
                t == "compression_start" for t, _ in events2
            )
            assert has_compression_start, (
                "RED: 第二轮无 compression_start 帧。\n"
                "可能原因：needs_compression 标志虽翻正但段一未检测到。"
            )

            # RED 断言 ②：第二轮应有 compression_end 帧
            has_compression_end = any(
                t == "compression_end" for t, _ in events2
            )
            assert has_compression_end, (
                "RED: 第二轮无 compression_end 帧。压缩流程可能中断。"
            )

            # RED 断言 ③：DB 中应有 summary 行
            sid_uuid = uuid.UUID(sid)
            async with integration_runtime.db_session_factory() as db:
                summary_result = await db.execute(
                    select(Message).where(
                        Message.session_id == sid_uuid,
                        Message.role == MessageRole.summary,
                        Message.status == MessageStatus.active,
                    )
                )
                summary_row = summary_result.scalar_one_or_none()
                assert summary_row is not None, (
                    "RED: 压缩后应有 summary 行。"
                )
                assert len(summary_row.content) > 0, (
                    "RED: summary 行内容不应为空。"
                )

                # RED 断言 ④：旧消息应被标记为 compressed
                from sqlalchemy import func as _func
                compressed_count: int = (
                    await db.execute(
                        select(_func.count(Message.id)).where(
                            Message.session_id == sid_uuid,
                            Message.status == MessageStatus.compressed,
                        )
                    )
                ).scalar_one()
                # 第 1 轮 human + ai 至少 2 条应被压缩
                assert compressed_count >= 2, (
                    f"RED: 应有 ≥2 条消息被压缩，实际 {compressed_count}"
                )

        finally:
            clear_test_llm()
            if sid:
                await integration_redis.delete(f"chat:lock:{sid}")
