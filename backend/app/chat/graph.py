"""LangGraph 单节点流式图（M3 验证用）。

M6 临时版本：仅含 call_main_llm 单节点，
节点内部用 llm.astream 逐 chunk yield AIMessageChunk。
流式通路走 LangGraph custom streaming API（stream_mode="custom"），
不依赖 astream_events / on_chat_model_stream。

持久化与 DB 写入收敛到 me.py generator（Step 6/8b），此处不写任何 DB。
"""

from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.chat.factory import get_chat_llm


class ChatState(TypedDict):
    """M3 最小状态：只含消息列表。

    M6 会扩展为完整主对话图的 State（含 audit_state / session_id / child_profile 等）。
    """

    messages: Annotated[list[BaseMessage], add_messages]


ALLOWED_FINISH_REASONS = frozenset({"stop", "length", "content_filter"})


async def call_main_llm(state: ChatState) -> dict[str, list[BaseMessage]]:
    """唯一节点：调用 qwen3.5-flash 生成回复。

    使用 llm.astream（流式）逐 chunk yield，通过 LangGraph get_stream_writer()
    发送增量。流式由 stream_chat 的 custom stream mode 路径处理。

    finish_reason 透传：DashScope SDK `choice.finish_reason` 命中白名单
    stop / length / content_filter 时，写入末 chunk 的 response_metadata。
    其他值（tool_calls / function_call 等）不写 writer，由 stream_chat
    兜底 emit stop。

    注意：本节点不写 DB，DB 写入收敛到 me.py generator（Step 8b T5 唯一写入点）。
    """
    writer = get_stream_writer()
    llm = get_chat_llm()
    parts: list[str] = []
    async for chunk in llm.astream(state["messages"]):
        text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
        if text:
            writer({"delta": text})
            parts.append(text)
        # finish_reason 透传（白名单过滤；非白名单值不写 writer，stream_chat 兜底 stop）
        # AIMessageChunk.response_metadata is {}; real finish_reason in additional_kwargs
        ak = chunk.additional_kwargs or {}
        fr = ak.get("response_metadata", {}).get("finish_reason")
        if fr in ALLOWED_FINISH_REASONS:
            writer({"finish_reason": fr})
    return {"messages": [AIMessage(content="".join(parts))]}


def build_chat_graph():
    """构造 M3 主对话图（单节点），用于 dev_chat 兼容路径。"""
    builder = StateGraph(ChatState)
    builder.add_node("call_main_llm", call_main_llm)
    builder.add_edge(START, "call_main_llm")
    builder.add_edge("call_main_llm", END)
    return builder.compile()


# M6 主图导出名（Step 6 扩展为 5 节点图后替换）
main_graph = build_chat_graph()
