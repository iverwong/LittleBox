"""SSE 事件序列化 + LangGraph 流式事件转 SSE 帧。

双 framer 并存（禁止合并）：
- _sse_pack()        ：M3 单行协议 `data: {"type": ..., ...}\n\n`（供 dev_chat 兼容路径）
- _frame_sse_event()：M6 多行协议 `event: <type>\ndata: <json>\n\n`（供 me 主路径）

dev_chat 协议 4 类帧语义：
  start  → generator 入口 yield
  delta  → 每 content chunk；reasoning_content 不发进 delta（丢弃，仅作内部信号）
  error  → 捕获异常时 yield
  end    → astream 自然结束 / 消费完所有 chunk 后

stream_chat() 兼容入口走 main_graph.astream(..., stream_mode="custom")
的 AIMessageChunk 流，不再使用 astream_events / on_chat_model_stream。
"""
import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import anyio
from langchain_core.messages import AIMessageChunk, HumanMessage
from starlette.requests import ClientDisconnect

from app.chat.graph import build_chat_graph

logger = logging.getLogger(__name__)


# ---- M3 单行协议 framer（dev_chat 兼容路径） ----

def _sse_pack(event_type: str, **payload: Any) -> str:
    """SSE 单条消息序列化（M3 协议）。

    为什么不用 event: 字段而是 type 放 data 里：客户端统一 JSON 解析，少一层分支；
    后续要加新事件类型时不需要动前端 SSE 解析层。
    """
    body = json.dumps({"type": event_type, **payload}, ensure_ascii=False)
    return f"data: {body}\n\n"


# ---- M6 多行协议 framer（me 主路径） ----

def _frame_sse_event(event_type: str, data: dict) -> bytes:
    """SSE 多行协议帧（M6 协议）：event: <type>\ndata: <json>\n\n。"""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


async def stream_to_sse(graph_stream) -> AsyncIterator[bytes]:
    """将 AIMessageChunk 流转为 SSE 多行协议帧（me 主路径）。

    事件序列：
      reasoning chunk 首次到达 → thinking_start（仅信号，不含文本）
      reasoning 结束（首次出现 content chunk 或 reasoning 为空）→ thinking_end
      content chunk → delta

    注意：reasoning_content 不发进 delta（仅作内部信号，thinking_end 不带文本）。
    """
    thinking_started = False
    async for chunk in graph_stream:
        if not isinstance(chunk, AIMessageChunk):
            continue
        r = chunk.additional_kwargs.get("reasoning_content")
        c = chunk.content

        if r and not thinking_started:
            # reasoning chunk 首次到达 → emit thinking_start
            yield _frame_sse_event("thinking_start", {})
            thinking_started = True

        if thinking_started and not r:
            # reasoning 结束 → emit thinking_end（仅信号，不含文本）
            yield _frame_sse_event("thinking_end", {})
            thinking_started = False

        if c:
            yield _frame_sse_event("delta", {"content": c})

    # 流结束时尚未发送 thinking_end，补一个（防御性）
    if thinking_started:
        yield _frame_sse_event("thinking_end", {})


# ---- dev_chat 兼容入口（M3 单行协议） ----

async def stream_chat(user_message: str, session_id: str) -> AsyncIterator[str]:
    """将 LangGraph custom-stream 流式事件转换为 SSE M3 单行帧。

    dev_chat 协议：start / delta / error / end
    reasoning_content 不发进 delta（丢弃，仅作内部信号）。
    """
    yield _sse_pack("start", session_id=session_id)

    graph = build_chat_graph()
    try:
        async for event in graph.astream_events(
            {"messages": [HumanMessage(content=user_message)]},
            version="v2",
        ):
            if event["event"] == "on_chat_model_stream":
                chunk = event["data"].get("chunk")
                if chunk is not None and chunk.content:
                    yield _sse_pack("delta", content=chunk.content)
    except (asyncio.CancelledError, ClientDisconnect, anyio.BrokenResourceError):
        raise
    except Exception as exc:
        yield _sse_pack("error", message=str(exc), code=type(exc).__name__)
        return

    yield _sse_pack("end", finish_reason="stop")
