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
  1. StopWithAi：FakeLLM 产生 delta 后 stop → stopped 含 aid + DB ai 行
  2. StopNoAi：首 chunk 为空字符串 → has_emitted_content=False → stopped 无 aid
  3. best-effort 204：对不存在的 session 发 stop → 404

设计约束：
  - 双 ASGITransport：一个消费 SSE 流，一个发 stop HTTP 请求
  - running_streams entry 在 bg task finally 中被 pop，须在 pop 前发 stop
  - FakeLLM 须用 delay + 足够 chunks 使 bg task 存活窗口 > 0.5s
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.chat.factory import clear_test_llm, set_test_llm
from sqlalchemy import select

from ._helpers import FakeMainLLM, parse_sse_events, seed_integration_child

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


class TestStopTristateRed:
    """Stop 三态红测。"""

    @pytest.mark.xfail(strict=True, reason=(
        "Phase 2: load_audit_state 30s poll_wait 阻塞段一, stop 信号到达后段一仍卡在 poll_wait 中,"
        "无法在有效窗口完成。Phase 3 修入队名 bug 后 audit 信号就绪 → poll_wait 秒级返回 → 可测。"
    ))
    async def test_stop_with_ai(
        self,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        """StopWithAi：FakeLLM 产字后 stop → stopped 含 aid + ai 行 finish_reason=user_stopped。"""
        from httpx import ASGITransport, AsyncClient
        from app.main import create_app
        from app.auth.redis_client import get_redis
        from app.db import get_db
        from redis.asyncio import Redis

        child, headers = await seed_integration_child(integration_runtime)
        # delay=0.3, 8 chunks → bg task ~2.1s
        set_test_llm("deepseek", FakeMainLLM(
            chunks=["这", "是", "一", "段", "较", "长", "的", "回"],
            delay=0.3,
        ))

        app = create_app()
        app.state.resources = integration_runtime
        async def _get_db():
            async with integration_runtime.db_session_factory() as s:
                yield s
        async def _get_redis() -> Redis:
            return integration_runtime.audit_redis
        app.dependency_overrides[get_db] = _get_db
        app.dependency_overrides[get_redis] = _get_redis

        stream_t = ASGITransport(app=app)
        stop_t = ASGITransport(app=app)

        sid: str | None = None
        events: list[tuple[str, dict]] = []

        try:
            # Round 1：创建 session 获取 sid
            async with AsyncClient(transport=stop_t, base_url="http://test") as c:
                async with c.stream("POST", "/api/v1/me/chat/stream",
                    json={"content": "第一轮"}, headers=headers) as resp:
                    e1 = await parse_sse_events(resp)
                for t, d in e1:
                    if t == "session_meta":
                        sid = d["session_id"]
                        break
            assert sid is not None
            await integration_redis.delete(f"chat:throttle:{child.id}")
            import asyncio
            await asyncio.sleep(0.1)

            # Round 2：流消费 + 并发 stop
            async def _consume():
                nonlocal events
                async with AsyncClient(transport=stream_t, base_url="http://test") as sc:
                    async with sc.stream("POST", "/api/v1/me/chat/stream",
                        json={"content": "测试 stop"}, headers=headers) as resp:
                        events = await parse_sse_events(resp)

            consume_task = asyncio.create_task(_consume())
            await asyncio.sleep(0.6)
            async with AsyncClient(transport=stop_t, base_url="http://test") as stc:
                await stc.post(f"/api/v1/me/sessions/{sid}/stop", headers=headers)
            await consume_task

            if sid and str(sid) in integration_runtime._chat_tasks:
                t = integration_runtime._chat_tasks.get(str(sid))
                if t and not t.done():
                    await t

            stopped_events = [(t, d) for t, d in events if t == "stopped"]
            assert len(stopped_events) > 0, "RED: 无 stopped 事件 —— Stop 信号未生效"
            assert stopped_events[0][1].get("aid") is not None, (
                "RED: StopWithAi stopped 应含 aid"
            )
            if sid:
                sid_uuid = uuid.UUID(sid)
                async with integration_runtime.db_session_factory() as db:
                    from app.models.chat import Message, MessageRole
                    result = await db.execute(
                        select(Message).where(Message.session_id == sid_uuid,
                            Message.role == MessageRole.ai, Message.status == "active")
                        .order_by(Message.created_at.desc()))
                    ai_row = result.scalar_one_or_none()
                    assert ai_row is not None, "RED: StopWithAi 后应存在 ai 行"
                    assert ai_row.finish_reason == "user_stopped", (
                        f"RED: finish_reason 应为 'user_stopped'，实际 {ai_row.finish_reason!r}")
        finally:
            clear_test_llm()
            if sid:
                await integration_redis.delete(f"chat:lock:{sid}")

    @pytest.mark.xfail(strict=True, reason=(
        "Phase 2: 同 test_stop_with_ai，load_audit_state 30s poll_wait 阻塞段一，"
        "stop 无法在有效窗口完成。Phase 3 修入队名 bug 后可达。"
    ))
    async def test_stop_no_ai(
        self,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        """StopNoAi：首 chunk 为空 → has_emitted_content=False → stopped 无 aid"""
        from httpx import ASGITransport, AsyncClient
        from app.main import create_app
        from app.auth.redis_client import get_redis
        from app.db import get_db
        from redis.asyncio import Redis

        child, headers = await seed_integration_child(integration_runtime)
        set_test_llm("deepseek", FakeMainLLM(
            chunks=["", "后续内容", "更多内容", "测试"],
            delay=0.3,
        ))

        app = create_app()
        app.state.resources = integration_runtime
        async def _get_db():
            async with integration_runtime.db_session_factory() as s:
                yield s
        async def _get_redis() -> Redis:
            return integration_runtime.audit_redis
        app.dependency_overrides[get_db] = _get_db
        app.dependency_overrides[get_redis] = _get_redis

        stream_t = ASGITransport(app=app)
        stop_t = ASGITransport(app=app)
        sid: str | None = None
        events: list[tuple[str, dict]] = []

        try:
            async with AsyncClient(transport=stop_t, base_url="http://test") as c:
                async with c.stream("POST", "/api/v1/me/chat/stream",
                    json={"content": "第一轮"}, headers=headers) as resp:
                    e1 = await parse_sse_events(resp)
                for t, d in e1:
                    if t == "session_meta":
                        sid = d["session_id"]
                        break
            assert sid is not None
            await integration_redis.delete(f"chat:throttle:{child.id}")
            import asyncio
            await asyncio.sleep(0.1)

            async def _consume():
                nonlocal events
                async with AsyncClient(transport=stream_t, base_url="http://test") as sc:
                    async with sc.stream("POST", "/api/v1/me/chat/stream",
                        json={"content": "测试 stop"}, headers=headers) as resp:
                        events = await parse_sse_events(resp)

            consume_task = asyncio.create_task(_consume())
            await asyncio.sleep(0.5)
            async with AsyncClient(transport=stop_t, base_url="http://test") as stc:
                await stc.post(f"/api/v1/me/sessions/{sid}/stop", headers=headers)
            await consume_task

            if sid and str(sid) in integration_runtime._chat_tasks:
                t = integration_runtime._chat_tasks.get(str(sid))
                if t and not t.done():
                    await t

            stopped_events = [(t, d) for t, d in events if t == "stopped"]
            assert len(stopped_events) > 0, "RED: 无 stopped 事件 —— StopNoAi 未触发"
            assert stopped_events[0][1].get("aid") is None, (
                "RED: StopNoAi stopped 不应含 aid"
            )
            if sid:
                sid_uuid = uuid.UUID(sid)
                async with integration_runtime.db_session_factory() as db:
                    from app.models.chat import Message, MessageRole
                    result = await db.execute(
                        select(Message).where(Message.session_id == sid_uuid,
                            Message.role == MessageRole.ai))
                    assert len(result.scalars().all()) == 0, "RED: StopNoAi 后不应有 ai 行"
        finally:
            clear_test_llm()
            if sid:
                await integration_redis.delete(f"chat:lock:{sid}")

    async def test_best_effort_204(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        """best-effort 204：不存在的 session → 404（me.py:367 返回）。"""
        child, headers = await seed_integration_child(integration_runtime)
        fake_sid = "00000000-0000-0000-0000-000000000000"
        resp = await api_client.post(f"/api/v1/me/sessions/{fake_sid}/stop", headers=headers)
        assert resp.status_code == 404, f"应返回 404，实际 {resp.status_code}"
