"""stream_chat 端到端回归：mock LLM，流过 graph → SSE 帧。

路径 D（LangGraph custom stream mode）：
- call_main_llm 使用 get_stream_writer() + llm.astream() 逐 chunk 发增量
- stream_chat 使用 main_graph.astream(..., stream_mode="custom")
- 不依赖 astream_events / on_chat_model_stream
"""
import json

import pytest
from langchain_core.messages import AIMessageChunk

from app.chat import sse
from app.chat.factory import get_chat_llm


def _parse_sse_frames(raw_frames: list[str]) -> list[dict]:
    """解析 SSE M3 帧字符串列表为 dict 列表。"""
    parsed = []
    for frame in raw_frames:
        json_str = frame[len("data: "):]
        parsed.append(json.loads(json_str))
    return parsed


@pytest.fixture(autouse=True)
def _clear_llm_cache():
    """每个测试用例前后清除 lru_cache，避免 mock 失效。"""
    get_chat_llm.cache_clear()
    yield
    get_chat_llm.cache_clear()


# ---- E1: stream_chat 纯 content 流（路径 D）----

@pytest.mark.asyncio
async def test_stream_chat_emits_start_delta_end(monkeypatch) -> None:
    """mock LLM.astream 返回 2 个 content chunk → start + delta × 2 + end。"""
    fake_chunks = [
        AIMessageChunk(content="你", additional_kwargs={}),
        AIMessageChunk(content="好", additional_kwargs={}),
        AIMessageChunk(content="", additional_kwargs={"response_metadata": {"finish_reason": "stop"}}),
    ]

    class FakeLLM:
        async def astream(self, messages, options=None):
            for c in fake_chunks:
                yield c

    monkeypatch.setattr("app.chat.graph.get_chat_llm", lambda: FakeLLM())

    frames = [f async for f in sse.stream_chat("hi", "session-123")]

    parsed = _parse_sse_frames(frames)
    # 完整序列：start + delta("你") + delta("好") + end(finish_reason)
    assert len(parsed) == 4, f"expected 4 frames, got {len(parsed)}: {parsed}"
    assert parsed[0]["type"] == "start"
    assert parsed[0]["session_id"] == "session-123"
    assert parsed[1]["type"] == "delta"
    assert parsed[1]["content"] == "你"
    assert parsed[2]["type"] == "delta"
    assert parsed[2]["content"] == "好"
    assert parsed[3]["type"] == "end"
    assert parsed[3]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_stream_chat_delta_is_incremental(monkeypatch) -> None:
    """delta 帧是增量，不是累计拼接。"""
    fake_chunks = [
        AIMessageChunk(content="你", additional_kwargs={}),
        AIMessageChunk(content="好", additional_kwargs={}),
        AIMessageChunk(content="", additional_kwargs={"response_metadata": {"finish_reason": "stop"}}),
    ]

    class FakeLLM:
        async def astream(self, messages, options=None):
            for c in fake_chunks:
                yield c

    monkeypatch.setattr("app.chat.graph.get_chat_llm", lambda: FakeLLM())

    frames = [f async for f in sse.stream_chat("hi", "sid")]

    parsed = _parse_sse_frames(frames)
    delta_frames = [p for p in parsed if p["type"] == "delta"]
    assert len(delta_frames) == 2
    # 增量：每个 delta 只有单字，不是"你好"拼接
    assert delta_frames[0]["content"] == "你"
    assert delta_frames[1]["content"] == "好"


@pytest.mark.asyncio
async def test_stream_chat_empty_content_not_emitted(monkeypatch) -> None:
    """content 为空的 chunk 不发出 delta。"""
    # finish_reason chunk 本身 content 为空，不应发 delta
    fake_chunks = [
        AIMessageChunk(content="hello", additional_kwargs={}),
        AIMessageChunk(content="", additional_kwargs={"response_metadata": {"finish_reason": "stop"}}),
    ]

    class FakeLLM:
        async def astream(self, messages, options=None):
            for c in fake_chunks:
                yield c

    monkeypatch.setattr("app.chat.graph.get_chat_llm", lambda: FakeLLM())

    frames = [f async for f in sse.stream_chat("hi", "sid")]

    parsed = _parse_sse_frames(frames)
    delta_frames = [p for p in parsed if p["type"] == "delta"]
    assert len(delta_frames) == 1
    assert delta_frames[0]["content"] == "hello"
    assert parsed[-1]["type"] == "end"


# ---- E2: stream_chat reasoning + content 双流 ----

@pytest.mark.asyncio
async def test_stream_chat_reasoning_not_in_delta(monkeypatch) -> None:
    """reasoning_content chunk（content 为空）不出现在 delta 帧。"""
    fake_chunks = [
        AIMessageChunk(content="", additional_kwargs={"reasoning_content": "thinking..."}),
        AIMessageChunk(content="答案是42", additional_kwargs={}),
        AIMessageChunk(content="", additional_kwargs={"response_metadata": {"finish_reason": "stop"}}),
    ]

    class FakeLLM:
        async def astream(self, messages, options=None):
            for c in fake_chunks:
                yield c

    monkeypatch.setattr("app.chat.graph.get_chat_llm", lambda: FakeLLM())

    frames = [f async for f in sse.stream_chat("hi", "sid")]

    parsed = _parse_sse_frames(frames)
    delta_frames = [p for p in parsed if p["type"] == "delta"]
    assert len(delta_frames) == 1
    assert delta_frames[0]["content"] == "答案是42"
    for d in delta_frames:
        assert "think" not in d["content"].lower()
        assert "thinking" not in d["content"]


# ---- E3: stream_chat error 帧 ----

@pytest.mark.asyncio
async def test_stream_chat_error_on_exception(monkeypatch) -> None:
    """graph 执行异常时 yield error 帧。"""

    class FakeLLM:
        async def astream(self, messages, options=None):
            # Must be async generator (yield) so async for can iterate;
            # raising directly makes the coroutine invalid for async iteration
            yield AIMessageChunk(content="partial", additional_kwargs={})
            raise RuntimeError("upstream error")

    monkeypatch.setattr("app.chat.graph.get_chat_llm", lambda: FakeLLM())

    frames = [f async for f in sse.stream_chat("hi", "sid")]

    parsed = _parse_sse_frames(frames)
    # First frame is start; second is either delta (partial before error) or error
    # Either way we must see error frame with the correct message
    error_frames = [p for p in parsed if p["type"] == "error"]
    assert len(error_frames) == 1, f"expected 1 error frame, got {parsed}"
    assert "upstream error" in error_frames[0]["message"]


# ---- E4: finish_reason 白名单 ----

@pytest.mark.asyncio
async def test_stream_chat_finish_reason_length(monkeypatch) -> None:
    """finish_reason=length 白名单透传。"""
    fake_chunks = [
        AIMessageChunk(content="hello", additional_kwargs={}),
        AIMessageChunk(content="", additional_kwargs={"response_metadata": {"finish_reason": "length"}}),
    ]

    class FakeLLM:
        async def astream(self, messages, options=None):
            for c in fake_chunks:
                yield c

    monkeypatch.setattr("app.chat.graph.get_chat_llm", lambda: FakeLLM())

    frames = [f async for f in sse.stream_chat("hi", "sid")]

    parsed = _parse_sse_frames(frames)
    end_frames = [p for p in parsed if p["type"] == "end"]
    assert len(end_frames) == 1
    assert end_frames[0]["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_stream_chat_finish_reason_tool_calls_not_emitted(monkeypatch) -> None:
    """finish_reason=tool_calls 不在白名单，dashscope_chat 不写 response_metadata，
    graph.py 不发 finish_reason 帧，stream_chat 不发 end 帧。"""
    fake_chunks = [
        AIMessageChunk(content="hello", additional_kwargs={}),
        # tool_calls 不在白名单，chunk 无 response_metadata 无 finish_reason
    ]

    class FakeLLM:
        async def astream(self, messages, options=None):
            for c in fake_chunks:
                yield c

    monkeypatch.setattr("app.chat.graph.get_chat_llm", lambda: FakeLLM())

    frames = [f async for f in sse.stream_chat("hi", "sid")]

    parsed = _parse_sse_frames(frames)
    end_frames = [p for p in parsed if p["type"] == "end"]
    # tool_calls 不透传：既无 end 帧，也无 finish_reason
    assert len(end_frames) == 0
    # 有 delta（最后一个 content chunk）
    delta_frames = [p for p in parsed if p["type"] == "delta"]
    assert len(delta_frames) == 1
