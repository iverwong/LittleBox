"""ChatDashScopeQwen plain class 测试。"""
from unittest.mock import MagicMock, patch

import pytest
from dashscope.aigc.multimodal_conversation import AioMultiModalConversation
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk

from app.chat.dashscope_chat import (
    ChatDashScopeQwen,
    DashScopeAPIError,
    DashScopeCallOptions,
    SearchOptions,
)

# ---- 辅助：构造 mock SDK chunk ----

def _make_mock_chunk(
    content: str | list,
    finish_reason: str | None = None,
    reasoning_content: str | None = None,
    usage: MagicMock | None = None,
    status_code: int = 200,
) -> MagicMock:
    """构造一个假的 AioMultiModalConversation 响应 chunk。"""
    msg = MagicMock()
    msg.content = content
    msg.reasoning_content = reasoning_content
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason
    resp = MagicMock()
    resp.status_code = status_code
    resp.code = "InvalidParameter"
    resp.message = "test error"
    resp.request_id = "test-id"
    resp.output = MagicMock()
    resp.output.choices = [choice]
    resp.usage = usage
    return resp


def _make_usage(input_tok: int, output_tok: int, total_tok: int) -> MagicMock:
    u = MagicMock()
    u.input_tokens = input_tok
    u.output_tokens = output_tok
    u.total_tokens = total_tok
    return u


# ---- T1: DashScopeCallOptions 默认值 ----

def test_call_options_defaults() -> None:
    """DashScopeCallOptions 默认值：enable_thinking=True, enable_search=False。"""
    opts = DashScopeCallOptions()
    assert opts.enable_thinking is True
    assert opts.enable_search is False
    assert opts.result_format == "message"


def test_call_options_serialization_exclude_none() -> None:
    """model_dump(exclude_none=True) 不含 None 字段。"""
    opts = DashScopeCallOptions(enable_thinking=False, temperature=0.7)
    data = opts.model_dump(exclude_none=True)
    assert None not in data.values()
    assert data["enable_thinking"] is False
    assert data["temperature"] == 0.7
    assert "thinking_budget" not in data
    assert "search_options" not in data


def test_search_options_nested_serialization() -> None:
    """SearchOptions 嵌套模型序列化正确。"""
    opts = DashScopeCallOptions(
        enable_search=True,
        search_options=SearchOptions(search_strategy="auto", forced_search=True),
    )
    data = opts.model_dump(exclude_none=True)
    assert data["enable_search"] is True
    assert data["search_options"]["search_strategy"] == "auto"
    assert data["search_options"]["forced_search"] is True


# ---- T2: plain class — 不继承 BaseChatModel ----

def test_chat_dashscope_not_basechatmodel() -> None:
    """ChatDashScopeQwen 是 plain class，不继承 BaseChatModel。"""
    llm = ChatDashScopeQwen(model="qwen3.5-flash", api_key="sk-test")
    # 正确断言：不 instanceof BaseChatModel
    assert not isinstance(llm, BaseChatModel)
    # MRO 检查
    assert BaseChatModel not in type(llm).__mro__


# ---- T3: astream 返回 AIMessageChunk 流 ----

@pytest.mark.asyncio
async def test_astream_yields_aimessage_chunk_stream() -> None:
    """astream 返回 AsyncIterator[AIMessageChunk]。"""
    llm = ChatDashScopeQwen(model="qwen3.5-flash", api_key="sk-test")

    chunks = [
        _make_mock_chunk("你", usage=_make_usage(10, 1, 11)),
        _make_mock_chunk("好", usage=_make_usage(10, 2, 12)),
    ]

    async def mock_stream(*args, **kwargs):
        for c in chunks:
            yield c

    with patch.object(AioMultiModalConversation, "call", return_value=mock_stream()):
        result = [c async for c in llm.astream([MagicMock(type="human", content="hi")])]

    assert len(result) == 2
    assert all(isinstance(c, AIMessageChunk) for c in result)
    assert result[0].content == "你"
    assert result[1].content == "好"


# ---- T4: reasoning_content 分流 ----

@pytest.mark.asyncio
async def test_astream_reasoning_content_goes_to_additional_kwargs() -> None:
    """reasoning_content 写入
    AIMessageChunk.additional_kwargs["reasoning_content"]，content 为空。"""
    llm = ChatDashScopeQwen(model="qwen3.5-flash", api_key="sk-test")

    chunks = [
        _make_mock_chunk(
            "",
            reasoning_content="让我想想",
            usage=_make_usage(5, 0, 5),
        ),
        _make_mock_chunk(
            "答案是42",
            reasoning_content="我算完了",
            finish_reason="stop",
            usage=_make_usage(5, 3, 8),
        ),
    ]

    async def mock_stream(*args, **kwargs):
        for c in chunks:
            yield c

    with patch.object(AioMultiModalConversation, "call", return_value=mock_stream()):
        result = [c async for c in llm.astream([MagicMock(type="human", content="hi")])]

    # reasoning chunk
    assert result[0].content == ""
    assert result[0].additional_kwargs.get("reasoning_content") == "让我想想"
    # content chunk
    assert result[1].content == "答案是42"
    assert result[1].additional_kwargs.get("reasoning_content") == "我算完了"


# ---- T5: finish_reason 透传 — 白名单三态 ----

@pytest.mark.asyncio
async def test_finish_reason_stop_whitelisted() -> None:
    """finish_reason=stop 命中白名单，写入 response_metadata。"""
    llm = ChatDashScopeQwen(model="qwen3.5-flash", api_key="sk-test")

    chunks = [
        _make_mock_chunk("hi", finish_reason="stop", usage=_make_usage(5, 2, 7)),
    ]

    async def mock_stream(*args, **kwargs):
        for c in chunks:
            yield c

    with patch.object(AioMultiModalConversation, "call", return_value=mock_stream()):
        result = [c async for c in llm.astream([MagicMock(type="human", content="hi")])]

    # 末 chunk 有 response_metadata
    assert result[-1].additional_kwargs.get("response_metadata") == {"finish_reason": "stop"}


@pytest.mark.asyncio
async def test_finish_reason_length_whitelisted() -> None:
    """finish_reason=length 命中白名单，写入 response_metadata。"""
    llm = ChatDashScopeQwen(model="qwen3.5-flash", api_key="sk-test")

    chunks = [
        _make_mock_chunk("hi", finish_reason="length", usage=_make_usage(5, 100, 105)),
    ]

    async def mock_stream(*args, **kwargs):
        for c in chunks:
            yield c

    with patch.object(AioMultiModalConversation, "call", return_value=mock_stream()):
        result = [c async for c in llm.astream([MagicMock(type="human", content="hi")])]

    assert result[-1].additional_kwargs.get("response_metadata") == {"finish_reason": "length"}


@pytest.mark.asyncio
async def test_finish_reason_content_filter_whitelisted() -> None:
    """finish_reason=content_filter 命中白名单，写入 response_metadata。"""
    llm = ChatDashScopeQwen(model="qwen3.5-flash", api_key="sk-test")

    chunks = [
        _make_mock_chunk("hi", finish_reason="content_filter", usage=_make_usage(5, 2, 7)),
    ]

    async def mock_stream(*args, **kwargs):
        for c in chunks:
            yield c

    with patch.object(AioMultiModalConversation, "call", return_value=mock_stream()):
        result = [c async for c in llm.astream([MagicMock(type="human", content="hi")])]

    assert (
        result[-1].additional_kwargs.get("response_metadata")
        == {"finish_reason": "content_filter"}
    )


# ---- T6: finish_reason 不透传 — 非白名单 ----

@pytest.mark.asyncio
async def test_finish_reason_tool_calls_not_whitelisted() -> None:
    """finish_reason=tool_calls 不在白名单，不写入 response_metadata。"""
    llm = ChatDashScopeQwen(model="qwen3.5-flash", api_key="sk-test")

    chunks = [
        _make_mock_chunk("hi", finish_reason="tool_calls", usage=_make_usage(5, 2, 7)),
    ]

    async def mock_stream(*args, **kwargs):
        for c in chunks:
            yield c

    with patch.object(AioMultiModalConversation, "call", return_value=mock_stream()):
        result = [c async for c in llm.astream([MagicMock(type="human", content="hi")])]

    assert "response_metadata" not in result[-1].additional_kwargs


@pytest.mark.asyncio
async def test_finish_reason_none_not_whitelisted() -> None:
    """中间 chunk finish_reason=None（DashScope SDK 中间帧），不写入 response_metadata。"""
    llm = ChatDashScopeQwen(model="qwen3.5-flash", api_key="sk-test")

    chunks = [
        _make_mock_chunk("你", finish_reason=None, usage=_make_usage(10, 1, 11)),
        _make_mock_chunk("好", finish_reason="stop", usage=_make_usage(10, 2, 12)),
    ]

    async def mock_stream(*args, **kwargs):
        for c in chunks:
            yield c

    with patch.object(AioMultiModalConversation, "call", return_value=mock_stream()):
        result = [c async for c in llm.astream([MagicMock(type="human", content="hi")])]

    # 第一块无 finish_reason
    assert "response_metadata" not in result[0].additional_kwargs
    # 第二块有 stop
    assert result[-1].additional_kwargs.get("response_metadata") == {"finish_reason": "stop"}


# ---- T7: ainvoke 收集完整响应 ----

@pytest.mark.asyncio
async def test_ainvoke_collects_full_response() -> None:
    """ainvoke 返回完整 AIMessage，内容为所有 content chunk 的拼接。"""
    llm = ChatDashScopeQwen(model="qwen3.5-flash", api_key="sk-test")

    chunks = [
        _make_mock_chunk("你", reasoning_content="想一下", usage=_make_usage(10, 1, 11)),
        _make_mock_chunk("好", usage=_make_usage(10, 2, 12)),
        _make_mock_chunk("呀", finish_reason="stop", usage=_make_usage(10, 3, 13)),
    ]

    async def mock_stream(*args, **kwargs):
        for c in chunks:
            yield c

    with patch.object(AioMultiModalConversation, "call", return_value=mock_stream()):
        result = await llm.ainvoke([MagicMock(type="human", content="hi")])

    assert isinstance(result, AIMessage)
    assert result.content == "你好呀"
    # reasoning_content 在 additional_kwargs，不在 content
    assert result.additional_kwargs.get("reasoning_content") == "想一下"
    assert "reasoning_content" not in result.content


# ---- T8: SDK 非 200 → DashScopeAPIError ----

@pytest.mark.asyncio
async def test_non_200_raises_dashscope_api_error() -> None:
    """SDK 返回非 200 时抛出 DashScopeAPIError，不塞进 AIMessage。"""
    llm = ChatDashScopeQwen(model="qwen3.5-flash", api_key="sk-test")

    error_chunk = _make_mock_chunk(
        "err", status_code=401, usage=_make_usage(0, 0, 0)
    )

    async def mock_stream(*args, **kwargs):
        yield error_chunk

    with patch.object(AioMultiModalConversation, "call", return_value=mock_stream()):
        with pytest.raises(DashScopeAPIError) as exc_info:
            await llm.ainvoke([MagicMock(type="human", content="hi")])

    assert exc_info.value.code == "InvalidParameter"


# ---- T9: get_chat_llm 单例 ----

def test_get_chat_llm_returns_singleton() -> None:
    """get_chat_llm() 返回同一实例。"""
    from app.chat.dashscope_chat import get_chat_llm

    get_chat_llm.cache_clear()
    llm1 = get_chat_llm()
    llm2 = get_chat_llm()
    assert llm1 is llm2
