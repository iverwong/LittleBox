"""LangGraph 流式事件验证测试（mock LLM，不走真网络）。"""

import asyncio

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app.chat.graph import build_chat_graph, call_main_llm
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


# ---- D6: event order: on_chain_start → on_chat_model_stream* → on_chain_end ----

@pytest.mark.asyncio
async def test_graph_events_ordered_start_then_streams_then_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """锁定事件顺序：on_chain_start → on_chat_model_stream* → on_chain_end。"""
    get_chat_llm.cache_clear()
    monkeypatch.setattr(
        "app.chat.graph.get_chat_llm",
        lambda: GenericFakeChatModel(messages=iter([AIMessage(content="hello")])),
    )

    graph = build_chat_graph()
    events = [
        e async for e in graph.astream_events(
            {"messages": [HumanMessage(content="hi")]},
            version="v2",
        )
    ]

    target_events = [
        e["event"] for e in events
        if e["event"] in ("on_chain_start", "on_chat_model_stream", "on_chain_end")
    ]

    assert target_events[0] == "on_chain_start", "first event must be on_chain_start"
    assert target_events[-1] == "on_chain_end", "last event must be on_chain_end"
    assert any(e == "on_chat_model_stream" for e in target_events), \
        "must have at least one on_chat_model_stream"

    # all on_chat_model_stream indices are between first and last
    stream_indices = [i for i, e in enumerate(target_events) if e == "on_chat_model_stream"]
    assert all(0 < i < len(target_events) - 1 for i in stream_indices), \
        "all on_chat_model_stream events must be between start and end"

    # on_chain_start name includes call_main_llm
    start_events = [e for e in events if e["event"] == "on_chain_start"]
    assert any("call_main_llm" in e.get("name", "") for e in start_events), \
        "on_chain_start should reference call_main_llm node"


# ---- D7: node exception propagates to caller ----

class _ExplodingChatModel(GenericFakeChatModel):
    """GenericFakeChatModel with ainvoke override that throws."""

    def __init__(self):
        super().__init__(messages=iter([]), _agenerate=False, _astream=False)

    async def ainvoke(self, input, **kwargs):
        raise RuntimeError("node boom")


@pytest.mark.asyncio
async def test_graph_node_exception_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """锁定节点异常：ainvoke 抛出的异常必须从 astream_events 重抛。

    on_chain_error 事件取决于 LangGraph 版本和错误触发路径；
    核心契约是异常向上传播，而非被静默吞没。
    """
    get_chat_llm.cache_clear()
    monkeypatch.setattr("app.chat.graph.get_chat_llm", lambda: _ExplodingChatModel())

    graph = build_chat_graph()

    # 异常必须向上抛出（RuntimeError 被 pytest.raises 捕获）
    with pytest.raises(RuntimeError, match="node boom"):
        async for _ in graph.astream_events(
            {"messages": [HumanMessage(content="hi")]},
            version="v2",
        ):
            pass


# ---- D8: add_messages reducer appends not overwrites ----

@pytest.mark.asyncio
async def test_graph_messages_reducer_appends_not_overwrites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """锁定 ChatState.messages 的 add_messages reducer：追加语义，非覆盖。"""
    get_chat_llm.cache_clear()
    monkeypatch.setattr(
        "app.chat.graph.get_chat_llm",
        lambda: GenericFakeChatModel(messages=iter([AIMessage(content="你好")])),
    )

    graph = build_chat_graph()
    result = await graph.ainvoke({"messages": [HumanMessage(content="hi")]})

    assert len(result["messages"]) == 2, "messages should have 2 entries (input + output)"
    assert result["messages"][0].type == "human"
    assert result["messages"][0].content == "hi"
    assert result["messages"][1].type == "ai"
    assert result["messages"][1].content == "你好"
