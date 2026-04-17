"""qwen3.5-flash 真实调用烟雾测试（需要真实 API key）。"""
import pytest
from langchain_core.messages import AIMessage

from app.chat.llm import get_chat_llm


@pytest.mark.live
@pytest.mark.asyncio
async def test_qwen_flash_ainvoke_returns_text():
    """验证 ChatDashScopeQwen 在 Python 3.14 + DashScope SDK 下可正常调用 qwen3.5-flash。

    断言强化：
    1. usage_metadata.input_tokens > 0（防止错误消息被塞进 content 导致的假阳性）
    2. finish_reason ∈ {"stop", "length"}（防止非终止态被错误透传）
    3. reasoning_content 不出现在返回的 AIMessage 中（验证 enable_thinking=False 生效）
    """
    llm = get_chat_llm()
    result = await llm.ainvoke("用一句话回答：今天心情不错用一个词描述是什么？")
    assert isinstance(result, AIMessage)
    assert isinstance(result.content, str) and len(result.content) > 0
    assert result.usage_metadata is not None
    assert result.usage_metadata["input_tokens"] > 0
    assert result.response_metadata.get("finish_reason") in {"stop", "length"}
    # reasoning_content 不应出现在 AIMessage 的任意属性中（enable_thinking=False 生效）
    # type: ignore[reportAttributeAccessIssue] — AIMessage 类型 stub 未定义此字段，但 enable_thinking=True 时 SDK 会注入
    assert getattr(result, "reasoning_content", None) is None
