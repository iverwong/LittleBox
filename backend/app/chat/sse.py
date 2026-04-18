"""SSE 事件序列化 + LangGraph 流式事件转 SSE 帧。"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import anyio
from langchain_core.messages import HumanMessage
from starlette.requests import ClientDisconnect

from app.chat.graph import build_chat_graph


def _sse_pack(event_type: str, **payload: Any) -> str:
    """SSE 单条消息序列化。

    为什么不用 event: 字段而是 type 放 data 里：客户端统一 JSON 解析，少一层分支；
    后续要加新事件类型时不需要动前端 SSE 解析层。
    """
    body = json.dumps({"type": event_type, **payload}, ensure_ascii=False)
    return f"data: {body}\n\n"


async def stream_chat(user_message: str, session_id: str) -> AsyncIterator[str]:
    """将 LangGraph 流式事件转换为 SSE 帧。"""
    yield _sse_pack("start", session_id=session_id)

    graph = build_chat_graph()
    try:
        async for event in graph.astream_events(
            {"messages": [HumanMessage(content=user_message)]},
            version="v2",
        ):
            # on_chat_model_stream 是 LangChain 定义的 token 级事件
            # data.chunk 是 AIMessageChunk，content 为本次增量文本
            if event["event"] == "on_chat_model_stream":
                chunk = event["data"].get("chunk")
                if chunk is not None and chunk.content:
                    yield _sse_pack("delta", content=chunk.content)
    except (asyncio.CancelledError, ClientDisconnect, anyio.BrokenResourceError):
        # 客户端断开：asyncio.CancelledError 由 Starlette StreamingResponse 传播；
        # BrokenResourceError 为防御性捕获（anyio 写失败时触发）。
        # 不发 error 事件（连接已关，写入会回爆堆栈）。
        raise
    except Exception as exc:  # noqa: BLE001 —— SSE 错误透传需要兜住所有上游真实异常
        yield _sse_pack("error", message=str(exc), code=type(exc).__name__)
        return

    yield _sse_pack("end", finish_reason="stop")
