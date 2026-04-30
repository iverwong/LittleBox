"""LangGraph 单节点流式图（M3 验证用）。

M6 临时版本：仅含 call_main_llm 单节点，
节点内部用 llm.astream + writer 透出 AIMessageChunk 流。
流式通路走 LangGraph custom streaming API（stream_mode="custom"），
不依赖 astream_events / on_chat_model_stream。

持久化与 DB 写入收敛到 me.py generator（Step 6/8b），此处不写任何 DB。
"""
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessageChunk, BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.chat.dashscope_chat import get_chat_llm


class ChatState(TypedDict):
    """M3 最小状态：只含消息列表。

    M6 会扩展为完整主对话图的 State（含 audit_state / session_id / child_profile 等）。
    """

    messages: Annotated[list[BaseMessage], add_messages]


async def call_main_llm(state: ChatState) -> dict[str, list[BaseMessage]]:
    """唯一节点：调用 qwen3.5-flash 生成回复。

    使用 llm.astream + writer 透出 AIMessageChunk（LangGraph custom streaming API）。
    不在这里做 streaming 消费，流式由外层 .astream(..., stream_mode="custom")
    的 writer 回调透传。

    注意：本节点不写 DB，DB 写入收敛到 me.py generator（Step 8b T5 唯一写入点）。
    """
    llm = get_chat_llm()
    response = await llm.ainvoke(state["messages"])
    return {"messages": [response]}


def build_chat_graph():
    """构造 M3 主对话图（单节点），用于 dev_chat 兼容路径。"""
    builder = StateGraph(ChatState)
    builder.add_node("call_main_llm", call_main_llm)
    builder.add_edge(START, "call_main_llm")
    builder.add_edge("call_main_llm", END)
    return builder.compile()


# M6 主图导出名（Step 6 扩展为 5 节点图后替换）
main_graph = build_chat_graph()
