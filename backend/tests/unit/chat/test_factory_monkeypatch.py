"""Monkeypatch 单元测试：验证 factory.py 模块级 monkeypatch 行为。

覆盖三个验证维度：
1. 确认 monkeypatch 已生效（_convert_message_to_dict 被替换）
2. 确认 reasoning_content 被正确保留
3. 确认版本断言在不匹配版本时抛 AssertionError
"""
from __future__ import annotations

from unittest.mock import patch

from langchain_core.messages import AIMessage
import pytest


class TestMonkeypatchApplied:
    """验证 factory.py 导入后 monkeypatch 已生效。"""

    def test_convert_function_replaced(self) -> None:
        """导入 factory 后，模块级的 _convert_message_to_dict 应被替换。"""
        import app.core.llm  # noqa: F401 — 触发模块级 monkeypatch

        import langchain_openai.chat_models.base as lcoai

        assert lcoai._convert_message_to_dict.__name__ == "_patched_convert"

    def test_reasoning_content_preserved(self) -> None:
        """AIMessage 含 reasoning_content → 序列化后 dict 含 reasoning_content 键。"""
        import app.core.llm  # noqa: F401

        import langchain_openai.chat_models.base as lcoai

        msg = AIMessage(
            content="hello",
            additional_kwargs={"reasoning_content": "思考过程"},
        )
        result = lcoai._convert_message_to_dict(msg)
        assert result.get("reasoning_content") == "思考过程"

    def test_no_reasoning_content_not_added(self) -> None:
        """AIMessage 无 reasoning_content → 序列化后不含该键。"""
        import app.core.llm  # noqa: F401

        import langchain_openai.chat_models.base as lcoai

        msg = AIMessage(content="hello")
        result = lcoai._convert_message_to_dict(msg)
        assert result.get("reasoning_content") is None

    def test_tool_calls_still_works(self) -> None:
        """验证 monkeypatch 不影响 tool_calls 序列化。"""
        import app.core.llm  # noqa: F401

        import langchain_openai.chat_models.base as lcoai

        msg = AIMessage(
            content="",
            tool_calls=[{"name": "test_tool", "args": {"x": 1}, "id": "call-1"}],
        )
        result = lcoai._convert_message_to_dict(msg)
        assert "tool_calls" in result


class TestVersionAssertion:
    """版本断言在错误版本时抛 AssertionError。"""

    def test_wrong_version_raises_assertion_error(self) -> None:
        """mock 版本号 → importlib.reload 应触发生效断言检查。"""
        import importlib
        import app.core.llm  # noqa: F811

        with patch(
            "importlib.metadata.version",
            return_value="9.9.9",
        ):
            with pytest.raises(AssertionError, match="未经验证"):
                importlib.reload(app.core.llm)


class TestPositionalArgumentSafety:
    """monkeypatch 的位置参数兼容性守护。

    LangChain 内部如果某条代码路径以位置参数调用
    _convert_message_to_dict(msg, "chat/completions")，
    patched 函数必须能正确透传，否则 TypeError。
    """

    def test_positional_api_arg_preserves_reasoning(self) -> None:
        """用位置参数调 patched 函数，断言不 raise + reasoning_content 仍保留。"""
        import app.core.llm  # noqa: F401

        import langchain_openai.chat_models.base as lcoai

        msg = AIMessage(
            content="hello",
            additional_kwargs={"reasoning_content": "思考过程"},
        )
        # 位置参数调用 (message, api)
        result = lcoai._convert_message_to_dict(msg, "chat/completions")
        assert result.get("reasoning_content") == "思考过程"

    def test_positional_api_arg_no_reasoning(self) -> None:
        """位置参数调用，无 reasoning_content 时不出错。"""
        import app.core.llm  # noqa: F401

        import langchain_openai.chat_models.base as lcoai

        msg = AIMessage(content="hello")
        result = lcoai._convert_message_to_dict(msg, "chat/completions")
        assert result.get("reasoning_content") is None
        assert result["role"] == "assistant"
