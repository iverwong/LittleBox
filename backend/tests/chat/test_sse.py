"""_frame_sse_event + stream_graph_to_sse 测试。

M6 多行协议 framer 和 graphSSE 桥的单元测试。
M3 dev 路径（_sse_pack / stream_chat）已于 M6-patch3 移除。
"""

import json

import pytest

from app.chat import sse


# ---- S2: _frame_sse_event 格式（M6 多行协议）----


def test_frame_sse_event_format() -> None:
    """_frame_sse_event 产出 M6 多行协议格式：event: <type>\ndata: <json>\n\n。"""
    result = sse._frame_sse_event("thinking_start", {})
    assert result.startswith(b"event: thinking_start\ndata: ")
    assert result.endswith(b"\n\n")
    parsed = json.loads(result[len("event: thinking_start\ndata: "):])
    assert parsed == {}


def test_frame_sse_event_with_payload() -> None:
    """_frame_sse_event 含 data dict 序列化。"""
    result = sse._frame_sse_event("delta", {"content": "hi"})
    assert b"event: delta\ndata: " in result
    lines = result.split(b"\n")
    data_line = lines[1]
    json_str = data_line.replace(b"data: ", b"", 1)
    parsed = json.loads(json_str)
    assert parsed == {"content": "hi"}


# ---- S4: patch3 清理后仅保留 _frame_sse_event（契约保护）----


def test_frame_sse_event_only_remains() -> None:
    """_frame_sse_event 保留，_sse_pack 和 stream_chat 已移除（patch3 清理契约）。"""
    assert hasattr(sse, '_frame_sse_event')
    assert not hasattr(sse, '_sse_pack')
    assert not hasattr(sse, 'stream_chat')


# ---- S6: stream_graph_to_sse 三分支直接单元测试 ----


@pytest.mark.asyncio
async def test_stream_graph_to_sse_dict_with_delta() -> None:
    """dict payload 含 delta => yield 完整 SSE 帧。"""
    async def _gen():
        yield {"delta": "x"}

    frames = [f async for f in sse.stream_graph_to_sse(_gen())]
    assert len(frames) == 1
    assert frames[0] == b'event: delta\ndata: {"content": "x"}\n\n'


@pytest.mark.asyncio
async def test_stream_graph_to_sse_dict_no_delta() -> None:
    """dict payload 无 delta => yield 序列为空。"""
    async def _gen():
        yield {"other": 1}

    frames = [f async for f in sse.stream_graph_to_sse(_gen())]
    assert frames == []


@pytest.mark.parametrize("payload", [None, 42, "str", [], object()])
@pytest.mark.asyncio
async def test_stream_graph_to_sse_non_dict(payload) -> None:
    """非 dict payload 参数化 => yield 序列均为空。"""
    async def _gen():
        yield payload

    frames = [f async for f in sse.stream_graph_to_sse(_gen())]
    assert frames == []
