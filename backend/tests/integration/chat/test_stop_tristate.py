"""Step 17 · 红测：Stop 三态 — `rr.main_graph.astream` monkeypatch 范式。

范式来源（tests/api/test_chat_stream_stop_keepgo.py）：
  monkeypatch `integration_runtime.main_graph.astream` 为 fake async generator，
  在精确点位调用 `running_streams[str(sid)].set()` 触发 stop。
  由于 fake astream 替换了整张图（含 load_audit_state），既无 poll_wait 阻塞，
  也无 ASGITransport 时序问题——running_streams entry 在 handler 中已创建，
  fake astream 在 handler 返回后即被 bg task 调用，此时 entry 存活。

三种状态：
  - StopWithAi：先 yield delta → set → yield finish_reason → stopped 含 aid + DB ai 行
  - StopNoAi：先 set（无 delta）→ yield finish_reason → stopped 无 aid + 无 DB ai 行
  - best-effort 204：stop endpoint 不存在的 session → 404
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.core.llm import clear_test_llm
from app.domain.chat.stream_signals import running_streams

from ._helpers import seed_integration_child

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
]


def _parse_sse(raw: str) -> list[dict]:
    """Parse SSE multi-line text into list of {type, data} dicts."""
    events: list[dict] = []
    for part in raw.strip().split("\n\n"):
        if not part.strip():
            continue
        lines = part.strip().split("\n")
        ev_type = None
        ev_data = None
        for line in lines:
            if line.startswith("event:"):
                ev_type = line[6:].strip()
            elif line.startswith("data:"):
                ev_data = json.loads(line[5:].strip())
        if ev_type and ev_data is not None:
            events.append({"type": ev_type, "data": ev_data})
    return events


class TestStopTristateRed:
    """Stop 三态 — `rr.main_graph.astream` monkeypatch。"""

    async def test_stop_with_ai(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        """StopWithAi：fake 先 yield delta → 设 stop → DB ai 行 finish_reason=user_stopped。"""
        child, headers = await seed_integration_child(integration_runtime)

        async def _fake_astream(initial_state, stream_mode="custom", **kwargs):
            yield {"delta": "Hello"}
            ctx = kwargs.get("context")
            sid = str(ctx.session_id) if ctx else None
            ev = running_streams.get(sid)
            if ev is not None:
                ev.set()
            yield {"finish_reason": "stop"}

        integration_runtime.main_graph.astream = _fake_astream
        try:
            resp = await api_client.post(
                "/api/v1/me/chat/stream",
                json={"content": "测试 stop"},
                headers=headers,
            )
            assert resp.status_code == 200
        finally:
            clear_test_llm()

        events = _parse_sse(resp.text)

        sid = next(
            (e["data"]["session_id"] for e in events if e["type"] == "session_meta"),
            None,
        )

        stopped = [e for e in events if e["type"] == "stopped"]
        assert len(stopped) > 0, "RED: 无 stopped 事件"
        assert stopped[0]["data"].get("aid") is not None, (
            "RED: StopWithAi 的 stopped 应含 aid"
        )
        assert not any(e["type"] == "end" for e in events), (
            "RED: StopWithAi 不应有 end 帧"
        )

        from uuid import UUID
        from app.models.chat import Message, MessageRole
        from sqlalchemy import select

        async with integration_runtime.db_session_factory() as db:
            msgs = (
                (await db.execute(
                    select(Message).where(
                        Message.session_id == UUID(sid),
                    ).order_by(Message.created_at)
                ))
                .scalars()
                .all()
            )
            assert len(msgs) == 2, f"应有 2 行，实际 {len(msgs)}"
            ai = msgs[1]
            assert ai.role == MessageRole.ai, f"RED: 第二行应为 ai，实际 {ai.role}"
            assert ai.finish_reason == "user_stopped", (
                f"RED: finish_reason 应为 'user_stopped'，实际 {ai.finish_reason!r}"
            )

        assert sid not in running_streams, "running_streams 未清理"
        lock_exists = await integration_redis.exists(f"chat:lock:{sid}")
        assert not lock_exists, "session lock 未释放"

    async def test_stop_no_ai(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        """StopNoAi：fake 先设 stop → yield finish_reason → stopped 无 aid + 无 DB ai。"""
        child, headers = await seed_integration_child(integration_runtime)

        async def _fake_astream(initial_state, stream_mode="custom", **kwargs):
            ctx = kwargs.get("context")
            sid = str(ctx.session_id) if ctx else None
            ev = running_streams.get(sid)
            if ev is not None:
                ev.set()
            yield {"finish_reason": "stop"}

        integration_runtime.main_graph.astream = _fake_astream
        try:
            resp = await api_client.post(
                "/api/v1/me/chat/stream",
                json={"content": "测试 stop"},
                headers=headers,
            )
            assert resp.status_code == 200
        finally:
            clear_test_llm()

        events = _parse_sse(resp.text)

        sid = next(
            (e["data"]["session_id"] for e in events if e["type"] == "session_meta"),
            None,
        )

        stopped = [e for e in events if e["type"] == "stopped"]
        assert len(stopped) > 0, "RED: 无 stopped 事件 —— StopNoAi 未触发"
        assert stopped[0]["data"].get("aid") is None, (
            "RED: StopNoAi 的 stopped 不应含 aid"
        )
        assert not any(e["type"] == "end" for e in events), (
            "RED: StopNoAi 不应有 end 帧"
        )

        from uuid import UUID
        from app.models.chat import Message, MessageRole
        from sqlalchemy import select

        async with integration_runtime.db_session_factory() as db:
            msgs = (
                (await db.execute(
                    select(Message).where(
                        Message.session_id == UUID(sid),
                    ).order_by(Message.created_at)
                ))
                .scalars()
                .all()
            )
            assert len(msgs) == 1, f"应有 1 行，实际 {len(msgs)}"
            assert msgs[0].role == MessageRole.human

        assert sid not in running_streams, "running_streams 未清理"
        lock_exists = await integration_redis.exists(f"chat:lock:{sid}")
        assert not lock_exists, "session lock 未释放"

    async def test_best_effort_204(
        self,
        api_client: Any,
        integration_runtime: Any,
        integration_redis: Any,
    ) -> None:
        """best-effort：不存在的 session → 404。"""
        child, headers = await seed_integration_child(integration_runtime)
        resp = await api_client.post(
            "/api/v1/me/sessions/00000000-0000-0000-0000-000000000000/stop",
            headers=headers,
        )
        assert resp.status_code == 404, f"应返回 404，实际 {resp.status_code}"
