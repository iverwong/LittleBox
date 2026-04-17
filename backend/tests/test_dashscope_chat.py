"""ChatDashScopeQwen 薄包装测试。"""
from unittest.mock import MagicMock, patch

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
async def test_astream_yields_delta_chunks():
    """_astream 应逐块 yield delta。"""
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
    assert result_chunks[0].text == "你"
    assert result_chunks[1].text == "好"
    assert result_chunks[2].text == ""  # finish chunk


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


def _make_usage(input_tok: int, output_tok: int, total_tok: int) -> MagicMock:
    u = MagicMock()
    u.input_tokens = input_tok
    u.output_tokens = output_tok
    u.total_tokens = total_tok
    return u
