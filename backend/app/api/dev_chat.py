"""M3 开发调试路由。M7 聊天界面正式版上线后整文件删除。"""

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.chat.sse import stream_chat

router = APIRouter(prefix="/api/dev", tags=["dev"])


class DevChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


@router.post("/chat/stream")
async def dev_chat_stream(payload: DevChatRequest, request: Request) -> StreamingResponse:
    """M3 Demo：单轮流式对话，不鉴权、不落库。

    客户端断开由 Starlette 自动传播 CancelledError 到 stream_chat 生成器，
    由 httpx 客户端（`dashscope` SDK 底层）释放上游连接 —— 无需手动 is_disconnected 轮询。
    """
    session_id = str(uuid.uuid4())
    return StreamingResponse(
        stream_chat(payload.message, session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 防止 nginx / 反代缓冲
            "Connection": "keep-alive",
        },
    )
