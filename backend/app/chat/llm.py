"""LLM 单例构造器。"""
from functools import lru_cache

from app.chat.dashscope_chat import ChatDashScopeQwen
from app.config import settings


@lru_cache(maxsize=1)
def get_chat_llm() -> ChatDashScopeQwen:
    """构造主对话 LLM 单例。

    为什么走 DashScope 原生端而不是百炼 OpenAI 兼容端：
    - 兼容端对内置工具（code_interpreter / web_search / MCP）支持不完整，长期需切 Responses API；
    - `langchain-qwq` 在 M3 Step 3 实测百炼兼容端 401；
    - DashScope 原生 SDK 由阿里官方维护，Qwen 新特性首发通道，可控性最高。

    为什么关闭思考模式：M3 做流式链路验证，思考阶段产生的 reasoning_content
    会延迟首个 content token 的到达，干扰"首 token 延迟"这一核心验证指标。
    M6 / M8 再根据场景决定是否启用。
    """
    return ChatDashScopeQwen(
        model="qwen3.5-flash",
        api_key=settings.dashscope_api_key,
        enable_thinking=False,
        timeout=30,
        max_retries=0,
    )
