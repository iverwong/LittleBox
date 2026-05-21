"""多轮 reasoning_content 序列化集成测试。

验证：在安装了 factory.py monkeypatch 后，多轮 agentic loop 中
第二轮的 request body messages 数组里前一条 assistant message 含非空 reasoning_content。

模拟场景：
- 第一轮：LLM 返回含 tool_calls 的 AIMessage，additional_kwargs 含 reasoning_content
- Tool 处理后返回 ToolMessage
- 第二轮：将完整消息历史重发给 LLM，验证序列化后的请求体中
  第一条 assistant message 含 reasoning_content 字段
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


@pytest.mark.asyncio
async def test_multiround_request_contains_reasoning_content() -> None:
    """验证多轮第二轮 request body 含 reasoning_content。

    策略：hook 住 LangChain 的 _convert_message_to_dict，收集第二轮序列化结果，
    断言 assistant message 含 reasoning_content。
    """
    import app.chat.factory  # noqa: F401 — 确保 monkeypatch 已生效

    import langchain_openai.chat_models.base as lcoai

    # hook _convert_message_to_dict 收集每次序列化的完整 messages 列表
    original_convert = lcoai._convert_message_to_dict
    convert_results: list[dict] = []

    def _tracking_convert(message, **kwargs):
        result = original_convert(message, **kwargs)
        convert_results.append(result)
        return result

    lcoai._convert_message_to_dict = _tracking_convert

    try:
        # 构造多轮消息历史
        messages = [
            HumanMessage(content="你好，我今天心情不好"),
            AIMessage(
                content="",
                tool_calls=[{"name": "test_tool", "args": {"x": 1}, "id": "call-1"}],
                additional_kwargs={"reasoning_content": "用户表达负面情绪，需要关注"},
            ),
            ToolMessage(content='{"ok": true}', tool_call_id="call-1"),
            HumanMessage(content="继续说说我的感受"),
        ]

        # 将原 messages 序列化到一个列表中跟踪
        convert_results.clear()

        # 模拟第二轮 LLM 调用：逐个序列化 messages
        for msg in messages:
            _tracking_convert(msg, api="chat/completions")

        # 找到 assistant message 的序列化结果
        assistant_results = [
            r for r in convert_results if r.get("role") == "assistant"
        ]

        # 第一条 assistant message（带 tool_calls 的那条）应含 reasoning_content
        assert len(assistant_results) >= 1, "应该有至少一条 assistant message"
        first_assistant = assistant_results[0]
        assert "reasoning_content" in first_assistant, (
            "第一条 assistant message 应含 reasoning_content，"
            f"实际 keys={list(first_assistant.keys())}"
        )
        assert first_assistant["reasoning_content"] == "用户表达负面情绪，需要关注"

    finally:
        # 恢复 hook
        lcoai._convert_message_to_dict = original_convert


@pytest.mark.asyncio
async def test_multiround_without_monkeypatch_missing_reasoning() -> None:
    """反例验证：临时卸载 monkeypatch 后 reasoning_content 应丢失。

    验证 monkeypatch 确有必要——没有它时 reasoning_content 会丢。
    """
    import langchain_openai.chat_models.base as lcoai

    # 临时恢复原函数
    original_convert = lcoai._convert_message_to_dict
    from app.chat.factory import _orig_convert

    lcoai._convert_message_to_dict = _orig_convert

    try:
        msg = AIMessage(
            content="",
            tool_calls=[{"name": "t", "args": {}, "id": "c1"}],
            additional_kwargs={"reasoning_content": "思考过程"},
        )
        result = lcoai._convert_message_to_dict(msg)
        assert result.get("reasoning_content") is None, (
            "无 monkeypatch 时 reasoning_content 应丢失"
        )
    finally:
        lcoai._convert_message_to_dict = original_convert
