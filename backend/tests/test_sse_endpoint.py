"""SSE 端点集成测试（mock LLM，不走真网络）。"""

import asyncio
import json
import logging
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

from app.main import app


def _parse_sse_lines(body: str) -> list[dict]:
    """把 SSE 响应体拆成事件列表。"""
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


# ---- D10: happy path event schema locked ----

@pytest.mark.asyncio
async def test_sse_happy_path_event_schema_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    """锁定 happy path SSE 事件的字段 schema。"""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from app.chat.llm import get_chat_llm

    get_chat_llm.cache_clear()
    monkeypatch.setattr(
        "app.chat.graph.get_chat_llm",
        lambda: GenericFakeChatModel(messages=iter([AIMessage(content="你好")])),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/dev/chat/stream", json={"message": "hi"})
        assert resp.status_code == 200
        events = _parse_sse_lines(resp.text)

    # start: {type, session_id}，session_id 可 parse 为 uuid
    start = events[0]
    assert set(start.keys()) == {"type", "session_id"}, f"start keys: {set(start.keys())}"
    assert start["type"] == "start"
    uuid.UUID(start["session_id"])  # 不抛则合法

    # deltas: each {type, content}，content 为非空 str
    deltas = [e for e in events if e["type"] == "delta"]
    assert len(deltas) >= 1, "must have at least one delta"
    for d in deltas:
        assert set(d.keys()) == {"type", "content"}, f"delta keys: {set(d.keys())}"
        assert isinstance(d["content"], str) and d["content"], f"delta content: {d['content']!r}"

    # end: {type, finish_reason}，finish_reason == "stop"
    end = events[-1]
    assert set(end.keys()) == {"type", "finish_reason"}, f"end keys: {set(end.keys())}"
    assert end["type"] == "end"
    assert end["finish_reason"] == "stop"


# ---- D11: error event schema and no end after error ----

@pytest.mark.asyncio
async def test_sse_error_event_schema_and_no_end_after_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """锁定 error 事件 schema + error 后不再发 end（源码 return 实现）。"""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from app.chat.llm import get_chat_llm

    get_chat_llm.cache_clear()

    def error_getter() -> GenericFakeChatModel:
        raise RuntimeError("upstream error")

    monkeypatch.setattr("app.chat.graph.get_chat_llm", error_getter)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/dev/chat/stream", json={"message": "hi"})
        assert resp.status_code == 200
        events = _parse_sse_lines(resp.text)

    # error event schema
    err = next(e for e in events if e["type"] == "error")
    assert set(err.keys()) == {"type", "message", "code"}, f"error keys: {set(err.keys())}"
    assert err["code"] == "RuntimeError"
    assert "upstream error" in err["message"]

    # error 后无 end（源码 return 实现）
    assert not any(e["type"] == "end" for e in events), "error path should not yield end"


# ---- D9: disconnect path no error/end + caplog clean ----

@pytest.mark.asyncio
async def test_sse_disconnect_path(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """客户端断开：start 存在 + 无 error + 无 end + 无 CancelledError/BrokenResourceError 日志。"""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from app.chat.llm import get_chat_llm

    get_chat_llm.cache_clear()
    monkeypatch.setattr(
        "app.chat.graph.get_chat_llm",
        lambda: GenericFakeChatModel(messages=iter([AIMessage(content="你好")])),
    )

    caplog.set_level(logging.ERROR)  # 捕获 uvicorn.error logger

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream("POST", "/api/dev/chat/stream", json={"message": "hi"}) as resp:
            assert resp.status_code == 200
            events = []
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: "):]))
                    if events[-1]["type"] == "delta":
                        break  # 收到首个 delta 后断开

        # D9 assertions: start exists, no error, no end
        assert any(e["type"] == "start" for e in events), "must have start event"
        assert not any(e["type"] == "error" for e in events), f"disconnect must not emit error: {[e for e in events if e['type'] == 'error']}"
        assert not any(e["type"] == "end" for e in events), "disconnect must not emit end"

        # 等待 ≤ 0.2s 让 server 端 task 清理完成
        await asyncio.sleep(0.2)

        # caplog 无 CancelledError / BrokenResourceError
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        for r in error_records:
            msg = r.exc_text or ""
            assert "CancelledError" not in msg, f"CancelledError in exc_text: {msg}"
            assert "BrokenResourceError" not in msg, f"BrokenResourceError in exc_text: {msg}"


# ---- D12: message length validation (min=1, max=2000) ----

@pytest.mark.asyncio
async def test_dev_chat_stream_rejects_invalid_message_length() -> None:
    """DevChatRequest min_length=1 / max_length=2000 校验拦截。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 空消息 → 422
        resp = await client.post("/api/dev/chat/stream", json={"message": ""})
        assert resp.status_code == 422, f"empty should be 422, got {resp.status_code}"

        # 超长消息 → 422
        resp = await client.post("/api/dev/chat/stream", json={"message": "x" * 2001})
        assert resp.status_code == 422, f"2001 chars should be 422, got {resp.status_code}"


# ---- existing tests (kept as-is) ----

@pytest.mark.asyncio
async def test_sse_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常流：至少包含 start / delta / end 三类事件。"""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from app.chat.llm import get_chat_llm

    # 注意：必须 patch **使用点** app.chat.graph.get_chat_llm（Python import 绑定陷阱）
    get_chat_llm.cache_clear()
    monkeypatch.setattr(
        "app.chat.graph.get_chat_llm",
        lambda: GenericFakeChatModel(messages=iter([AIMessage(content="你好，小盒子")])),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/dev/chat/stream", json={"message": "hi"})
        assert resp.status_code == 200
        events = _parse_sse_lines(resp.text)

    assert events[0]["type"] == "start"
    assert any(e["type"] == "delta" for e in events)
    assert events[-1]["type"] == "end"


@pytest.mark.asyncio
async def test_sse_error_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """上游异常：SSE 响应包含 error 事件。"""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from app.chat.llm import get_chat_llm

    get_chat_llm.cache_clear()

    def error_getter() -> GenericFakeChatModel:
        raise RuntimeError("upstream error")

    monkeypatch.setattr("app.chat.graph.get_chat_llm", error_getter)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/dev/chat/stream", json={"message": "hi"})
        assert resp.status_code == 200
        events = _parse_sse_lines(resp.text)

    assert events[0]["type"] == "start"
    assert any(e["type"] == "error" for e in events)
    error_event = next(e for e in events if e["type"] == "error")
    assert "upstream error" in error_event["message"]
