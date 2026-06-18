"""frame_sse_event 测试。

M6 多行协议 framer 单元测试。
stream_graph_to_sse 和 frame_delta 已于 M9-patch3 移除。
"""

import json

import pytest
from app.domain.chat import stream as sse

# ---- S2: frame_sse_event 格式（M6 多行协议）----


def test_frame_sse_event_format() -> None:
    """frame_sse_event 产出 M6 多行协议格式：event: <type>\ndata: <json>\n\n。"""
    result = sse.frame_sse_event("thinking_start", {})
    assert result.startswith(b"event: thinking_start\ndata: ")
    assert result.endswith(b"\n\n")
    parsed = json.loads(result[len("event: thinking_start\ndata: "):])
    assert parsed == {}


def test_frame_sse_event_with_payload() -> None:
    """frame_sse_event 含 data dict 序列化。"""
    result = sse.frame_sse_event("delta", {"content": "hi"})
    assert b"event: delta\ndata: " in result
    lines = result.split(b"\n")
    data_line = lines[1]
    json_str = data_line.replace(b"data: ", b"", 1)
    parsed = json.loads(json_str)
    assert parsed == {"content": "hi"}


# ---- S4: patch3 清理后仅保留 frame_sse_event（契约保护）----


def test_frame_sse_event_only_remains() -> None:
    """frame_sse_event 保留，stream_graph_to_sse 和 frame_delta 已移除。"""
    assert hasattr(sse, 'frame_sse_event')
    assert not hasattr(sse, 'stream_graph_to_sse')
    assert not hasattr(sse, 'frame_delta')
