"""SSE 事件序列化 + LangGraph 流式事件转 SSE 帧。

M6 多行协议 framer + graphSSE 桥。
M3 dev 路径（_sse_pack / stream_chat）已于 M6-patch3 移除。
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)


# ---- M6 多行协议 framer（me 主路径） ----


def _frame_sse_event(event_type: str, data: dict) -> bytes:
    """SSE 多行协议帧（M6 协议）：event: <type>\ndata: <json>\n\n。"""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


async def stream_graph_to_sse(payloads) -> AsyncIterator[bytes]:
    """将 LangGraph custom-stream dict payload 流转为 SSE 多行协议帧（me 主路径）。

    Graph writer 合同（Step 6 实施）：每个 chunk 发 dict
      {"delta": "text"}  → content chunk
      {"finish_reason": "stop"|"length"|"content_filter"} → finish reason 帧

    事件序列（当前实现）：
      delta chunk   → delta
      finish_reason → 更新内部变量（透传给调用方，适配器不直接 emit end）
      流结束       → 无额外帧（end 帧由调用方 emit）

    Note: reasoning_content branch removed in Step 8b because me.py
    generator sends each payload individually to stream_graph_to_sse
    (per-payload _payloads() wrapper), making cross-payload state
    tracking (thinking_started) impossible.  If future graph forwards
    reasoning chunks, refactor to a long-lived async iterable pattern
    instead of the current per-payload wrapper.
    """
    async for payload in payloads:
        if not isinstance(payload, dict):
            continue
        d = payload.get("delta")
        if d:
            yield _frame_sse_event("delta", {"content": d})
