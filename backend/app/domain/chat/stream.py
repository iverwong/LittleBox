"""SSE 协议帧 + StreamingResponse generator。

从 `api/me.py` + `app/chat/sse.py` 整合而来。本模块对外公开:

- `frame_sse_event(event_type, data)` —— SSE 多行协议字节帧
- `build_flow_pause_frame(reason)` —— SSE backpressure 断流帧
- `stream_generator(queue, state, sid)` —— queue 帧转发 + overflow / 客户端断检测
- `ChatStreamState` —— 消费协程与本 generator 共享的轻量 mutable 容器(overflow flag)

多行协议 framer 单一定义:`event: <type>\ndata: <json>\n\n`。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID

import anyio

logger = logging.getLogger(__name__)


def frame_sse_event(event_type: str, data: dict) -> bytes:
    """组装 SSE 多行协议字节帧。

    帧格式:`event: <type>\ndata: <json>\n\n`。

    Args:
        event_type: SSE 事件类型(对应前端 `addEventListener` 的事件名)。
        data: 序列化为 JSON 的负载字典。

    Returns:
        编码后的 UTF-8 字节。
    """
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def build_flow_pause_frame(reason: str = "backpressure") -> bytes:
    """构造 `flow_pause` SSE 帧,SSE generator 在 backpressure 断流时发射。

    帧格式(复用多行协议):
      event: flow_pause
      data: {"reason": "backpressure"}

    类型由 `event:` 行承载,data 内不放 `type` 字段(与 delta 帧同构)。
    前端 `react-native-sse` 靠 `event:` 名分发到 `es.addEventListener('flow_pause')`;
    缺 `event:` 行会落进默认 `message` 事件被静默丢弃。

    Args:
        reason: 触发 flow_pause 的原因字符串,序列化进 data。

    Returns:
        编码后的 SSE 字节帧。
    """
    return frame_sse_event("flow_pause", {"reason": reason})


# --- 消费协程与 SSE generator 共享状态 ---


@dataclass
class ChatStreamState:
    """消费协程与 SSE generator 共享的轻量 mutable 容器。

    Attributes:
        overflow: 入队侧翻正后,取帧侧应在 yield 前检测并断流。
    """

    overflow: bool = False


# --- SSE frame forwarder ---


async def stream_generator(
    queue: asyncio.Queue,
    state: ChatStreamState,
    sid: UUID,
) -> AsyncGenerator[bytes, None]:
    """SSE StreamingResponse generator。仅做帧转发 + overflow check + 客户端断检测。

    overflow check 在 await queue.get() 之前,避免以下时序陷阱:
    入队 put_nowait → QueueFull → 翻 overflow → 出队 queue.get() 取出后
    queue.full() 永远返回 False(size 已减 1),造成 overflow 漏检。

    首次帧超时保护:仅覆盖 session_meta 入队前的静默崩溃(如 db_session_factory
    初始化失败或消费协程 startup 异常)。session_meta 在进入 async-with 后第一句
    入队,生产毫秒级;10s 阈值不覆盖首 token 交付延迟。
    超时即静默退出(不做错误帧——消费协程已负责日志),避免请求级永久挂死。

    Args:
        queue: 与消费协程共享的 asyncio.Queue,字节帧从此处取出。
        state: 与消费协程共享的 ChatStreamState(overflow 标志)。
        sid: 当前 session UUID(用于日志关联)。

    Yields:
        编码后的 SSE 字节帧。
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
                # 消费协程未能在 10s 内产生首帧(含 session_meta),
                # 大概率是 bg task startup 静默崩溃;静默退出不做错误帧(消费协程已负责日志)
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
