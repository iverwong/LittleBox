"""DashScope qwen 系列薄包装，满足 LangChain BaseChatModel 接口。"""
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any, Literal

from dashscope.aigc.multimodal_conversation import AioMultiModalConversation
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.chat_models import (
    BaseChatModel,
    agenerate_from_stream,
)
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from pydantic import SecretStr


class DashScopeAPIError(Exception):
    """DashScope 非 200 响应统一抖出来。"""

    def __init__(self, code: str, message: str, request_id: str) -> None:
        super().__init__(f"[{code}] {message} (request_id={request_id})")
        self.code = code
        self.message = message
        self.request_id = request_id


class ChatDashScopeQwen(BaseChatModel):
    """DashScope qwen 系列薄包装（LangChain BaseChatModel）。

    M3 目标：仅实现 _astream + _agenerate 流式路径，
    验证 LangGraph .astream_events() 的 on_chat_model_stream 事件透传。
    其他特性（bind_tools / with_structured_output / 图像输入）均不实现。
    """

    model: str
    api_key: SecretStr
    enable_thinking: bool = False
    timeout: int = 30
    max_retries: int = 0
    # 固定为 False，防止下游误改导致 astream_events 退化为非流式
    # type: ignore[reportIncompatibleVariableOverride] — SDK type stub 声明 BaseChatModel.disable_streaming 为 bool|Literal["tool_calling"]，实际只用 False
    disable_streaming: bool | Literal["tool_calling"] = False

    @property
    def _llm_type(self) -> str:
        return "dashscope-qwen"

    def _to_dashscope_messages(self, messages: list[BaseMessage]) -> list[dict[str, Any]]:
        """将 LangChain 消息格式转换为 DashScope 多模态接口格式。

        DashScope 多模态接口要求 content 为 list[dict]，
        纯文本也要包成 [{"text": "..."}]。
        """
        role_map = {"human": "user", "ai": "assistant", "system": "system"}
        out: list[dict[str, Any]] = []
        for m in messages:
            text = m.content if isinstance(m.content, str) else str(m.content)
            out.append({"role": role_map[m.type], "content": [{"text": text}]})
        return out

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """流式调用 DashScope 多模态接口并逐 chunk yield ChatGenerationChunk。"""
        responses = await AioMultiModalConversation.call(
            model=self.model,
            api_key=self.api_key.get_secret_value(),
            messages=self._to_dashscope_messages(messages),
            result_format="message",
            stream=True,
            incremental_output=True,
            enable_thinking=self.enable_thinking,
        )
        async for response in responses:  # type: ignore[reportGeneralTypeIssues]
            if response.status_code != HTTPStatus.OK:
                raise DashScopeAPIError(response.code, response.message, response.request_id)

            choice = response.output.choices[0]
            raw = choice.message.content

            # content 可能为空 list（DashScope 思考阶段），或为 list[{"text": "..."}]
            if isinstance(raw, list):
                delta = "".join(part.get("text", "") for part in raw if isinstance(part, dict))
            else:
                delta = raw or ""

            # 空 delta 仍 yield 以保留 usage 累加时机（DashScope 每 chunk 带 usage）
            yield ChatGenerationChunk(message=AIMessageChunk(content=delta))

            # finish_reason 仅在末条 chunk 出现，此时透传最终 usage
            # 白名单判断，防止非终止态的 finish_reason（如 "nullnullstop"）被错误透传
            if choice.finish_reason in ("stop", "length", "tool_calls", "content_filter"):
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        response_metadata={"finish_reason": choice.finish_reason},
                        usage_metadata={
                            "input_tokens": response.usage.input_tokens,
                            "output_tokens": response.usage.output_tokens,
                            "total_tokens": response.usage.total_tokens,
                        },
                    ),
                )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return await agenerate_from_stream(
            self._astream(messages, stop, run_manager, **kwargs),
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        """同步 _generate 实现，仅为满足 BaseChatModel 抽象要求；实际使用 _astream / _agenerate。"""
        import asyncio

        return asyncio.get_event_loop().run_until_complete(
            self._agenerate(messages, stop, run_manager, **kwargs),
        )
