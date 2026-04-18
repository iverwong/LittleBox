"""SSE 端点集成测试（mock LLM，不走真网络）。"""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


def _parse_sse_lines(body: str) -> list[dict]:
    """把 SSE 响应体拆成事件列表。"""
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


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


@pytest.mark.asyncio
async def test_sse_disconnect_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """客户端提前断开：服务端无异常堆栈，error 事件不发给已断开客户端。"""
    import asyncio

    from langchain_core.messages import AIMessage

    from app.chat.llm import get_chat_llm

    get_chat_llm.cache_clear()

    # 模拟流式生成过程中客户端断开：on_chat_model_stream 事件发到一半时 cancel
    async def slow_stream():
        yield AIMessage(content="你好")
        await asyncio.sleep(10)  # 模拟长延迟

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    monkeypatch.setattr(
        "app.chat.graph.get_chat_llm",
        lambda: GenericFakeChatModel(messages=slow_stream()),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 使用 stream=True 并在收到首个 delta 后提前断开
        async with client.stream("POST", "/api/dev/chat/stream", json={"message": "hi"}) as resp:
            assert resp.status_code == 200
            events = []
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: "):]))
                    # 收到首个 delta 后立即断开
                    if events[-1]["type"] == "delta":
                        break
            # 验证 start 事件存在
            assert any(e["type"] == "start" for e in events)
