"""SSE 协议帧 + 段二 StreamingResponse generator。

Phase 2.2 从 `api/me.py` + `app/chat/sse.py` 整合而来。本模块对外公开:

- `frame_sse_event(event_type, data)` —— SSE 多行协议字节帧 (M6)
- `build_flow_pause_frame(reason)` —— 段二 backpressure 断流帧
- `stream_graph_to_sse(payloads)` —— LangGraph custom-stream dict → SSE 帧
- `stream_generator(queue, state, sid)` —— 段二:queue 帧转发 + overflow/客户端断检测
- `ChatStreamState` —— 段一段二共享的轻量 mutable 容器(overflow flag)
- `_stub_stream` —— LLM 桩流,仅供未来测试复用(死代码,Phase 6 之前保留)

M6 多行协议 framer 单一定义:event: <type>\\ndata: <json>\\n\\n。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import anyio

logger = logging.getLogger(__name__)


def frame_sse_event(event_type: str, data: dict) -> bytes:
    """SSE 多行协议帧(M6):event: <type>\\ndata: <json>\\n\\n。"""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def build_flow_pause_frame(reason: str = "backpressure") -> bytes:
    """构造 `flow_pause` SSE 帧,段二在 backpressure 断流时发射。

    帧格式(复用 M6 多行协议):
      event: flow_pause
      data: {"reason": "backpressure"}

    类型由 `event:` 行承载,data 内不放 `type` 字段(与 delta 帧同构)。
    前端 `react-native-sse` 靠 `event:` 名分发到 `es.addEventListener('flow_pause')`;
    缺 `event:` 行会落进默认 `message` 事件被静默丢弃。
    """
    return frame_sse_event("flow_pause", {"reason": reason})


async def stream_graph_to_sse(payloads) -> AsyncIterator[bytes]:
    """将 LangGraph custom-stream dict payload 流转为 SSE 多行协议帧(me 主路径)。

    Graph writer 合同(Step 6 实施):每个 chunk 发 dict
      {"delta": "text"}  → content chunk
      {"finish_reason": "stop"|"length"|"content_filter"} → finish reason 帧

    事件序列(当前实现):
      delta chunk   → delta
      finish_reason → 更新内部变量(透传给调用方,适配器不直接 emit end)
      流结束       → 无额外帧(end 帧由调用方 emit)

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
            yield frame_sse_event("delta", {"content": d})


# --- 死代码桩(Phase 2.2 决定保留,后续可清理) ---


async def _stub_stream() -> list[bytes]:
    """LLM 桩流:产生一个 delta 帧和一个 end 帧。当前生产代码无引用。"""
    return [
        frame_sse_event("delta", {"content": "[stub]"}),
        frame_sse_event("end", {"finish_reason": "stop", "aid": None}),
    ]


# --- 段一段二共享状态 ---


@dataclass
class ChatStreamState:
    """段一段二共享的轻量 mutable container。"""

    overflow: bool = False


# --- 段二: SSE frame forwarder ---


async def stream_generator(
    queue: asyncio.Queue,
    state: ChatStreamState,
    sid: UUID,
) -> AsyncGenerator[bytes, None]:
    """段二:StreamingResponse generator。仅做帧转发 + overflow check + 客户端断检测。

    overflow check(关注点 #3)在 await queue.get() 之前,避免以下时序陷阱:
    段一 put_nowait → QueueFull → 翻 overflow → 段二从 queue.get() 取出后
    queue.full() 永远返回 False(size 已减 1),造成 overflow 漏检。

    首次帧超时保护:仅覆盖 session_meta 入队前的静默崩溃(如 db_session_factory
    初始化失败或段一 startup 异常)。session_meta 在进入 async-with 后第一句
    入队,生产毫秒级;10s 阈值不覆盖首 token 交付延迟。
    超时即静默退出(不做错误帧 — 段一已负责日志),避免请求级永久挂死。
    """
    try:
        first_frame = True
        while True:
            if state.overflow:
                yield build_flow_pause_frame("backpressure")
                logger.info("sse backpressure cutoff", extra={"sid": str(sid)})
                return

            try:
                if first_frame:
                    frame = await asyncio.wait_for(queue.get(), timeout=10.0)
                    first_frame = False
                else:
                    frame = await queue.get()
            except asyncio.TimeoutError:
                # 段一未能在 10s 内产生首帧(含 session_meta),
                # 大概率是 bg task startup 静默崩溃;静默退出不做错误帧(段一已负责日志)
                logger.error(
                    "first frame timeout, bg task may have crashed silently",
                    extra={"sid": str(sid)},
                )
                return

            if frame is None:
                break

            try:
                yield frame
            except ConnectionError, anyio.BrokenResourceError:
                logger.info("client disconnected", extra={"sid": str(sid)})
                return
    except asyncio.CancelledError:
        raise
