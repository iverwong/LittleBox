"""DashScope qwen 系列薄包装 — Plain Class (不继承 BaseChatModel)。

设计决策（2026-04-30）：BaseChatModel 放弃使用，原因：
1. LangGraph nodes 不要求 BaseChatModel；
2. 不消耗 with_structured_output / LangSmith / with_fallbacks；
3. DashScopeCallOptions 直接传参比 bind()/model_kwargs 更清晰。
详见 架构基线 §十。

LangChain message types（AIMessageChunk 等）仅用作数据容器。
"""

from collections.abc import AsyncIterator
from functools import lru_cache
from http import HTTPStatus

from dashscope.aigc.multimodal_conversation import AioMultiModalConversation
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from pydantic import BaseModel, SecretStr


class SearchOptions(BaseModel):
    """搜索选项嵌套模型。"""

    search_strategy: str = "pro"
    enable_source: bool = True
    forced_search: bool = False
    search_prompt: str | None = None


class DashScopeCallOptions(BaseModel):
    """DashScope SDK 调用参数。"""

    enable_thinking: bool = True
    thinking_budget: int | None = None
    enable_search: bool = False
    search_options: SearchOptions | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    seed: int | None = None
    result_format: str = "message"


class DashScopeAPIError(Exception):
    """DashScope 非 200 响应统一抖出来。"""

    def __init__(self, code: str, message: str, request_id: str | None = None):
        super().__init__(f"DashScope API error {code}: {message}")
        self.code = code
        self.message = message
        self.request_id = request_id


class ChatDashScopeQwen:
    """DashScope qwen 系列薄包装 — Plain Class，不继承 BaseChatModel。

    暴露 astream / ainvoke 两个方法，直接操作 DashScope SDK 流。
    """

    model: str
    api_key: SecretStr

    def __init__(self, model: str, api_key: str | SecretStr):
        self._model = model
        self._api_key = SecretStr(api_key) if isinstance(api_key, str) else api_key

    async def astream(
        self,
        messages: list[BaseMessage],
        *,
        options: DashScopeCallOptions | None = None,
    ) -> AsyncIterator[AIMessageChunk]:
        """流式调用 DashScope 多模态接口，逐 chunk yield AIMessageChunk。

        reasoning 分流：
          - reasoning_content → AIMessageChunk.additional_kwargs["reasoning_content"]
          - content           → AIMessageChunk.content

        finish_reason 透传（逐 chunk 检查，白名单命中时写入 response_metadata）：
          - 白名单：stop / length / content_filter
          - 非白名单值（如 tool_calls / null / 空）不透传
        """
        opts = options or DashScopeCallOptions()
        sdk_params = opts.model_dump(exclude_none=True)
        sdk_messages = self._to_sdk_format(messages)

        responses = await AioMultiModalConversation.call(
            api_key=self._api_key.get_secret_value(),
            model=self._model,
            messages=sdk_messages,
            stream=True,
            incremental_output=True,
            **sdk_params,
        )

        async for response in responses:  # type: ignore[reportGeneralTypeIssues]
            self._check_error(response)
            yield self._to_ai_message_chunk(response)

    async def ainvoke(
        self,
        messages: list[BaseMessage],
        *,
        options: DashScopeCallOptions | None = None,
    ) -> AIMessage:
        """收集 astream 完整响应，返回完整 AIMessage。"""
        content = ""
        reasoning = ""
        async for chunk in self.astream(messages, options=options):
            # chunk.content is str | list per LangChain stubs; we always produce str
            chunk_content = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
            content += chunk_content
            reasoning += chunk.additional_kwargs.get("reasoning_content", "") or ""
        return AIMessage(
            content=content,
            additional_kwargs={"reasoning_content": reasoning} if reasoning else {},
        )

    @staticmethod
    def _to_sdk_format(messages: list[BaseMessage]) -> list[dict]:
        """将 LangChain 消息格式转换为 DashScope 多模态接口格式。

        DashScope 多模态接口要求 content 为 list[dict]，
        纯文本也要包成 [{"text": "..."}]。
        """
        role_map = {"human": "user", "ai": "assistant", "system": "system"}
        out = []
        for m in messages:
            text = m.content if isinstance(m.content, str) else str(m.content)
            out.append({"role": role_map[m.type], "content": [{"text": text}]})
        return out

    @staticmethod
    def _check_error(response) -> None:
        """检查响应状态码，非 200 则抛 DashScopeAPIError。"""
        if response.status_code != HTTPStatus.OK:
            raise DashScopeAPIError(
                code=response.code,
                message=response.message,
                request_id=getattr(response, "request_id", None),
            )

    @staticmethod
    def _to_ai_message_chunk(response) -> AIMessageChunk:
        """将 SDK 响应 chunk 转换为 AIMessageChunk。

        finish_reason 透传逻辑（逐 chunk 检查，白名单命中时写入）：
          - content 或 reasoning_content 每次都 yield
          - finish_reason 仅在 choice.finish_reason 命中白名单时写入
            response_metadata["finish_reason"]（白名单：stop / length / content_filter）
          - DashScope SDK 末帧才填 finish_reason，前面 chunk 为 None
        """
        choice = response.output.choices[0]
        msg = choice.message
        content_raw = msg.content

        # content 可能为 str / list[dict] / None
        if isinstance(content_raw, list):
            text = "".join(item.get("text", "") for item in content_raw if isinstance(item, dict))
        else:
            text = content_raw or ""

        reasoning = getattr(msg, "reasoning_content", None) or ""
        kwargs: dict = {}
        if reasoning:
            kwargs["reasoning_content"] = reasoning

        # finish_reason 透传：白名单逐 chunk 检查，非 None 且命中时写入
        fr = choice.finish_reason
        if fr is not None and fr in ("stop", "length", "content_filter"):
            kwargs["response_metadata"] = {"finish_reason": fr}

        return AIMessageChunk(content=text, additional_kwargs=kwargs)


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
    from app.config import settings

    return ChatDashScopeQwen(
        model="qwen3.5-flash",
        api_key=settings.dashscope_api_key,
    )
