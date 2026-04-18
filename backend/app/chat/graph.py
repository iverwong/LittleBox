"""LangGraph 单节点流式图（M3 验证用）。"""

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.chat.llm import get_chat_llm


class ChatState(TypedDict):
    """M3 最小状态：只含消息列表。

    M6 会扩展为完整主对话图的 State（含 audit_state / session_id / child_profile 等）。
    """

    messages: Annotated[list[BaseMessage], add_messages]


async def call_main_llm(state: ChatState) -> dict[str, list[BaseMessage]]:
    """唯一节点：调用 qwen3.5-flash 生成回复。

    不在这里做 streaming 消费，返回完整 AIMessage 即可；
    流式由外层 .astream_events() 从 on_chat_model_stream 事件透传。
    """
    llm = get_chat_llm()
    response = await llm.ainvoke(state["messages"])
    return {"messages": [response]}


def build_chat_graph():
    """构造 M3 主对话图（单节点）。"""
    builder = StateGraph(ChatState)
    builder.add_node("call_main_llm", call_main_llm)
    builder.add_edge(START, "call_main_llm")
    builder.add_edge("call_main_llm", END)
    return builder.compile()
