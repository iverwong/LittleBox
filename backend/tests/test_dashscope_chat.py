"""ChatDashScopeQwen 薄包装测试。"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dashscope.aigc.multimodal_conversation import AioMultiModalConversation

from app.chat.dashscope_chat import ChatDashScopeQwen, DashScopeAPIError


def _make_mock_chunk(
    content: list[dict],
    finish_reason: str | None = None,
    usage: MagicMock | None = None,
    status_code: int = 200,
) -> MagicMock:
    """构造一个假的 AioMultiModalConversation 响应 chunk。"""
    msg = MagicMock()
    msg.content = content
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


# ---- D1: fix duplicate assertion + lock finish chunk schema ----

@pytest.mark.asyncio
async def test_astream_yields_content_and_finish_chunks():
    """_astream 无条件 yield delta；仅在 finish_reason 白名单时追加 finish chunk（含 usage_metadata）。"""
    llm = ChatDashScopeQwen(
        model="qwen3.5-flash",
        api_key="sk-test",
        enable_thinking=False,
    )

    chunks = [
        _make_mock_chunk([{"text": "你"}], usage=_make_usage(10, 1, 11)),
        _make_mock_chunk([{"text": "好"}], usage=_make_usage(10, 2, 12)),
        _make_mock_chunk([], finish_reason="stop", usage=_make_usage(10, 2, 12)),
    ]

    async def mock_stream(*args, **kwargs):
        for c in chunks:
            yield c

    with patch.object(AioMultiModalConversation, "call", return_value=mock_stream()):
        result_chunks = [c async for c in llm._astream([MagicMock(type="human", content="hi")])]

    # _astream yields: delta("你"), delta("好"), empty(无finish), finish(含finish_reason+usage)
    # 4 chunks total (empty content chunk + finish chunk)
    assert len(result_chunks) == 4
    assert result_chunks[0].text == "你"
    assert result_chunks[1].text == "好"
    assert result_chunks[2].text == ""  # empty content chunk
    # finish chunk assertions (D1 new)
    assert result_chunks[3].text == ""
    assert result_chunks[3].message.response_metadata == {"finish_reason": "stop"}
    assert result_chunks[3].message.usage_metadata == {
        "input_tokens": 10,
        "output_tokens": 2,
        "total_tokens": 12,
    }


# ---- D2: empty content delta still yields ----

@pytest.mark.asyncio
async def test_astream_empty_content_still_yields_empty_delta():
    """DashScope 思考阶段 content=[] 仍 yield 空 delta，usage_metadata 在末条透传。"""
    llm = ChatDashScopeQwen(
        model="qwen3.5-flash",
        api_key="sk-test",
        enable_thinking=False,
    )

    chunks = [
        _make_mock_chunk([], usage=_make_usage(5, 0, 5)),  # 思考阶段，空 content
        _make_mock_chunk([{"text": "你好"}], finish_reason="stop", usage=_make_usage(5, 2, 7)),
    ]

    async def mock_stream(*args, **kwargs):
        for c in chunks:
            yield c

    with patch.object(AioMultiModalConversation, "call", return_value=mock_stream()):
        result_chunks = [c async for c in llm._astream([MagicMock(type="human", content="hi")])]

    # 空 delta / 实 delta / finish chunk
    assert len(result_chunks) == 3
    assert result_chunks[0].text == ""          # 空 delta
    assert result_chunks[1].text == "你好"       # 实 delta
    assert result_chunks[2].message.response_metadata["finish_reason"] == "stop"


# ---- D3: multi-part content concatenation ----

@pytest.mark.asyncio
async def test_astream_multi_content_parts_concatenated():
    """content 为 list[dict] 时，join 所有 text 片段，非 dict / 无 text key 的项被跳过。"""
    llm = ChatDashScopeQwen(
        model="qwen3.5-flash",
        api_key="sk-test",
        enable_thinking=False,
    )

    chunks = [
        _make_mock_chunk(
            [{"text": "hello"}, {"text": " world"}, {"other": "ignored"}, "string-should-skip"],
            finish_reason="stop",
            usage=_make_usage(8, 11, 19),
        ),
    ]

    async def mock_stream(*args, **kwargs):
        for c in chunks:
            yield c

    with patch.object(AioMultiModalConversation, "call", return_value=mock_stream()):
        result_chunks = [c async for c in llm._astream([MagicMock(type="human", content="hi")])]

    # "hello" + " world" = "hello world"；后两项被跳过
    assert len(result_chunks) == 2  # content + finish
    assert result_chunks[0].text == "hello world"


# ---- D4: non-whitelist finish_reason suppressed ----

@pytest.mark.asyncio
async def test_astream_non_whitelist_finish_reason_does_not_emit_finish_chunk():
    """finish_reason 不在白名单时，_astream 只 yield content，不追加 finish chunk。"""
    llm = ChatDashScopeQwen(
        model="qwen3.5-flash",
        api_key="sk-test",
        enable_thinking=False,
    )

    chunks = [
        _make_mock_chunk([{"text": "你"}], finish_reason="nullnullstop", usage=_make_usage(10, 1, 11)),
    ]

    async def mock_stream(*args, **kwargs):
        for c in chunks:
            yield c

    with patch.object(AioMultiModalConversation, "call", return_value=mock_stream()):
        result_chunks = [c async for c in llm._astream([MagicMock(type="human", content="hi")])]

    # 只有一个 content yield，无 finish chunk 追加
    assert len(result_chunks) == 1
    assert result_chunks[0].text == "你"
    assert result_chunks[0].message.response_metadata == {}  # 不含 finish_reason


# ---- D5: enable_thinking passed to SDK ----

@pytest.mark.asyncio
async def test_astream_passes_enable_thinking_to_sdk():
    """enable_thinking=True 时，SDK call 的 kwargs 必须含 enable_thinking=True。"""
    llm = ChatDashScopeQwen(
        model="qwen3.5-flash",
        api_key="sk-test",
        enable_thinking=True,
    )

    stop_chunk = _make_mock_chunk([{"text": "hi"}], usage=_make_usage(1, 2, 3))

    async def mock_stream(*args, **kwargs):
        yield stop_chunk

    with patch.object(AioMultiModalConversation, "call", return_value=mock_stream()) as mock_call:
        mock_call.return_value = mock_stream()
        async for _ in llm._astream([MagicMock(type="human", content="hi")]):
            pass

        mock_call.assert_called_once()
        kwargs = mock_call.call_args.kwargs
        assert kwargs["enable_thinking"] is True
        assert kwargs["stream"] is True
        assert kwargs["incremental_output"] is True
        assert kwargs["result_format"] == "message"


# ---- existing tests (kept as-is) ----

@pytest.mark.asyncio
async def test_agenerate_returns_valid_result():
    """_agenerate 应返回有效的 ChatResult。"""
    llm = ChatDashScopeQwen(
        model="qwen3.5-flash",
        api_key="sk-test",
        enable_thinking=False,
    )

    chunks = [
        _make_mock_chunk([{"text": "你"}], usage=_make_usage(10, 1, 11)),
        _make_mock_chunk([{"text": "好"}], usage=_make_usage(10, 2, 12)),
        _make_mock_chunk([], finish_reason="stop", usage=_make_usage(10, 2, 12)),
    ]

    async def mock_stream(*args, **kwargs):
        for c in chunks:
            yield c

    with patch.object(AioMultiModalConversation, "call", return_value=mock_stream()):
        result = await llm._agenerate([MagicMock(type="human", content="hi")])

    gen = result.generations[0]
    assert gen.message.content == "你好"


@pytest.mark.asyncio
async def test_non_200_raises_dashscope_api_error():
    """非 200 响应应抛出 DashScopeAPIError。"""
    llm = ChatDashScopeQwen(
        model="qwen3.5-flash",
        api_key="sk-test",
        enable_thinking=False,
    )

    error_chunk = _make_mock_chunk(
        [{"text": "err"}],
        status_code=401,
        usage=_make_usage(0, 0, 0),
    )

    async def mock_stream(*args, **kwargs):
        yield error_chunk

    with patch.object(AioMultiModalConversation, "call", return_value=mock_stream()):
        with pytest.raises(DashScopeAPIError) as exc_info:
            await llm._agenerate([MagicMock(type="human", content="hi")])

    assert exc_info.value.code == "InvalidParameter"


@pytest.mark.asyncio
async def test_content_as_str_also_handled():
    """content 可能为 str 也应能处理。"""
    llm = ChatDashScopeQwen(
        model="qwen3.5-flash",
        api_key="sk-test",
        enable_thinking=False,
    )

    chunks = [
        _make_mock_chunk("hello", usage=_make_usage(10, 5, 15)),
        _make_mock_chunk("", finish_reason="stop", usage=_make_usage(10, 5, 15)),
    ]

    async def mock_stream(*args, **kwargs):
        for c in chunks:
            yield c

    with patch.object(AioMultiModalConversation, "call", return_value=mock_stream()):
        result_chunks = [c async for c in llm._astream([MagicMock(type="human", content="hi")])]

    assert result_chunks[0].text == "hello"


@pytest.mark.asyncio
async def test_disable_streaming_is_false():
    """disable_streaming 必须为 False，确保流式路径不被意外关闭。"""
    llm = ChatDashScopeQwen(
        model="qwen3.5-flash",
        api_key="sk-test",
    )
    assert llm.disable_streaming is False