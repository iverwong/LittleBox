"""LangGraph 流式事件验证测试（mock LLM，不走真网络）。"""

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app.chat.graph import build_chat_graph
from app.chat.llm import get_chat_llm


def test_disable_streaming_is_false() -> None:
    """保护流式路径：禁止 disable_streaming 被误改导致 astream_events 退化。"""
    llm = get_chat_llm()
    assert llm.disable_streaming is False


def test_graph_stream_yields_on_chat_model_stream_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 .astream_events(version="v2") 能产出 on_chat_model_stream 事件。

    GenericFakeChatModel 支持流式，会触发 on_chat_model_stream。
    FakeListChatModel 不支持 streaming，不会触发该事件 —— 踩坑记录。
    """
    get_chat_llm.cache_clear()
    monkeypatch.setattr(
        "app.chat.graph.get_chat_llm",
        lambda: GenericFakeChatModel(
            messages=iter([AIMessage(content="你好，小盒子")]),
        ),
    )

    graph = build_chat_graph()
    events = []
    chunks = []

    async def run():
        async for event in graph.astream_events(
            {"messages": [HumanMessage(content="hi")]},
            version="v2",
        ):
            events.append(event)
            if event["event"] == "on_chat_model_stream":
                chunk = event["data"].get("chunk")
                if chunk is not None:
                    chunks.append(chunk)

    import asyncio

    asyncio.run(run())

    assert any(e["event"] == "on_chat_model_stream" for e in events), (
        "未捕获到 on_chat_model_stream 事件"
    )
    # chunk.content 应为本轮增量（GenericFakeChatModel 每次 yield 一个词）
    assert all(isinstance(c.content, str) for c in chunks), "chunk.content 应为字符串"


def test_graph_single_node_structure() -> None:
    """验证图结构：单节点 call_main_llm。"""
    graph = build_chat_graph()
    node_names = list(graph.nodes.keys())
    assert "call_main_llm" in node_names
    # START 是特殊入口节点，不在 .nodes 里；END 也同理
    # 只要 call_main_llm 节点存在即可
