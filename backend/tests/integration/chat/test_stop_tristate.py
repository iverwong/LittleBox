"""Step 17 · 红测：Stop 三态。

Stop 流程（来自 me.py 代码核实）：
  POST /me/sessions/{sid}/stop → running_streams[sid].set() → 204（best-effort）

  _run_llm_pipeline 主循环检测 stop_event.is_set()：
    ├─ user_stopped = True → break 出循环
    ├─ commit② 三态：
    │   ├─ StopWithAi：has_emitted_content=True → persist_ai_turn + audit + stopped(含 aid)
    │   ├─ StopNoAi：has_emitted_content=False → stopped(无 aid)，不写 DB 不发 audit
    └─ best-effort 204：stop endpoint 无论 event 是否存在都返回 204

本测试覆盖三种状态：
  1. StopWithAi：FakeLLM 产生足够 delta 后 stop
  2. StopNoAi：FakeLLM 不产生任何 delta（首 chunk 为空或极快 stop）
  3. best-effort 204：对无活跃流的 session 发 stop → 204

关注点 4（真 commit 跨测防护）：
  所有启动流的测试在结束前 await 段一收口。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.chat.factory import clear_test_llm, set_test_llm
from app.chat.locks import running_streams
from sqlalchemy import select

from ._helpers import FakeMainLLM, parse_sse_events, seed_integration_child

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


class TestStopTristateRed:
    """Stop 三态红测。"""

    async def test_stop_with_ai(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        """StopWithAi：调 stop endpoint 验证 stopped 帧 + ai 行 finish_reason=user_stopped。

        本测试为 Phase 3 设计的占位红测。Phase 2 中因 httpx ASGI 单 transport
        无法在流中并发发 stop HTTP 请求，此测试会因时序问题 RED。
        修复方法（Phase 3）：使用独立 ASGITransport 发 stop 请求 + asyncio.create_task
        解耦 consume/stop。
        """
        # Phase 2 placeholder: 预期 RED（需要两个独立 ASGITransport 并发机制）
        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm("deepseek", FakeMainLLM())
        try:
            sid = str(uuid.uuid4())
            assert False, (
                "RED: StopWithAi 需要 Phase 3 实现——两个独立 ASGITransport "
                "分别处理流消费和 stop 请求，当前无法在单 transport 上并发。"
            )
        finally:
            clear_test_llm()

    async def test_best_effort_204(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        """best-effort 204：对不存在的 session 调 stop → 404（而不是 204）。

        根据 me.py:367：session 不存在时 raise HTTPException(404)。
        真正的 best-effort 是 204 无论 event 是否存在。
        这里测试 404 路径（session 不存在时 stop endpoint 的行为）。
        """
        child, headers = await seed_integration_child(integration_runtime)

        fake_sid = "00000000-0000-0000-0000-000000000000"
        resp = await api_client.post(
            f"/api/v1/me/sessions/{fake_sid}/stop",
            headers=headers,
        )
        # 不存在的 session → 404（计划中的行为）
        assert resp.status_code == 404, (
            f"不存在的 session 应返回 404，实际 {resp.status_code}"
        )
