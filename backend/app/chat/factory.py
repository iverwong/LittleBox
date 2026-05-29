"""LLM provider factory: provider registry + ChatDeepSeek primary + fallback chain.

M6 patch 2 (Step 11.1): replaces the ChatOpenAI-only factory with a
_PROVIDER_REGISTRY dispatching by settings.main_provider. ChatDeepSeek
is the primary provider (preserving reasoning_content); ChatOpenAI is
registered for future M11+ experiments but not used in M6.
"""

from __future__ import annotations

import importlib.metadata as _metadata
from collections.abc import Callable
from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import Runnable
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

# ---- M8-hotfix: _convert_message_to_dict monkeypatch for reasoning_content ----
# 背景：langchain-openai 的 _convert_message_to_dict 序列化 AIMessage 时，
# 不会将 additional_kwargs.reasoning_content 传给 OpenAI API。DeepSeek 思考模式
# 要求：做过 tool_calls 的轮次后续请求必须回传 reasoning_content，否则 API 返回 400。
# 详见 LLM Provider 探针补4 多轮 agentic 用例。
# TODO: langchain-deepseek upstream PR 合入后移除本 monkeypatch。

_VERIFIED_LCO_VERSIONS = ("1.2.",)  # 当前已验证版本前缀
_lco_version = _metadata.version("langchain-openai")
assert any(
    _lco_version.startswith(v) for v in _VERIFIED_LCO_VERSIONS
), (
    f"langchain-openai 版本 {_lco_version} 未经验证，"
    f"_convert_message_to_dict monkeypatch 可能失效。"
    f"已验证版本前缀：{_VERIFIED_LCO_VERSIONS}。"
    f"升级版本前请重新跑 LLM Provider 探针的补4 用例。"
)

import langchain_openai.chat_models.base as _lcoai  # noqa: E402 — 必须 at 顶部之后（模块级副作用）

assert hasattr(_lcoai, "_convert_message_to_dict"), (
    "langchain_openai.chat_models.base._convert_message_to_dict 不存在，"
    "monkeypatch 失败。请检查 langchain-openai API 是否有变更。"
)

_orig_convert = _lcoai._convert_message_to_dict


def _patched_convert(message, *args, **kwargs):
    """补 langchain-openai 序列化时丢失 reasoning_content 的缺陷。

    DeepSeek 思考模式下，做过 tool_calls 的轮次后续请求必须回传
    reasoning_content，否则 API 返回 400。
    详见 LLM Provider 探针 补4。

    使用 *args, **kwargs 透传以兼容 LangChain 内部可能的位置参数调用。
    """
    result = _orig_convert(message, *args, **kwargs)
    if isinstance(message, AIMessage):
        rc = (message.additional_kwargs or {}).get("reasoning_content")
        if rc:
            result["reasoning_content"] = rc
    return result


_lcoai._convert_message_to_dict = _patched_convert
# ---- end monkeypatch ----


class ProviderNotRegisteredError(LookupError):
    """Provider 名未在 _PROVIDER_REGISTRY 中注册时抛出。"""


def _build_chat_deepseek(
    api_key: str,
    base_url: str,
    model: str,
    timeout: float,
    reasoning_effort: str,
    thinking_enabled: bool = True,
) -> ChatDeepSeek:
    """Construct ChatDeepSeek with thinking mode enabled.

    Note: ChatDeepSeek has its own ``api_base`` field (separate from
    ``BaseChatOpenAI.openai_api_base`` aliased from ``base_url``).
    The underlying OpenAI client reads ``api_base``, NOT ``openai_api_base``,
    so we pass ``api_base`` instead of ``base_url``.

    ``thinking_enabled`` (added M8 Step 4): controls whether ``extra_body``
    sets ``thinking.type=enabled`` or ``disabled``. Default ``True`` for
    backward compatibility with existing callers.
    """
    return ChatDeepSeek(
        api_key=api_key,  # type: ignore[arg-type]
        api_base=base_url,
        model=model,
        timeout=timeout,
        max_retries=0,  # SDK 内置重试关掉；统一由 with_retry 在应用层管理
        extra_body={
            "thinking": {"type": "enabled" if thinking_enabled else "disabled"},
            "reasoning_effort": reasoning_effort,
        },
    )


def _build_chat_openai(
    api_key: str,
    base_url: str,
    model: str,
    timeout: float,
) -> ChatOpenAI:
    """Construct ChatOpenAI (no thinking — reserved for M11+ experiments)."""
    return ChatOpenAI(
        api_key=api_key,  # type: ignore[arg-type]
        base_url=base_url,
        model=model,
        timeout=timeout,
        max_retries=0,  # SDK 内置重试关掉；统一由 with_retry 在应用层管理
    )


def _build_compression_deepseek(settings: Any) -> ChatDeepSeek:
    """压缩调用专用 DeepSeek 实例。thinking 默认关闭（compression_thinking_enabled=False）。"""
    return ChatDeepSeek(
        model=settings.compression_model,
        api_key=settings.deepseek_api_key.get_secret_value(),  # type: ignore[arg-type]
        api_base=settings.deepseek_base_url,
        timeout=settings.llm_request_timeout_seconds,
        max_retries=0,
        temperature=0.3,
        extra_body={
            "thinking": {
                "type": "enabled" if settings.compression_thinking_enabled else "disabled",
            },
        },
    )


_PROVIDER_REGISTRY: dict[str, Callable[..., Runnable]] = {
    "deepseek": lambda settings: _build_chat_deepseek(
        api_key=settings.deepseek_api_key.get_secret_value(),
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout=settings.llm_request_timeout_seconds,
        thinking_enabled=settings.main_thinking_enabled,
        reasoning_effort=settings.main_reasoning_effort,
    ),
    "openai": lambda settings: _build_chat_openai(
        api_key=settings.bailian_api_key.get_secret_value(),
        base_url=settings.bailian_base_url,
        model=settings.bailian_model,
        timeout=settings.llm_request_timeout_seconds,
    ),
    "audit_deepseek": lambda settings: _build_chat_deepseek(
        api_key=settings.deepseek_api_key.get_secret_value(),
        base_url=settings.deepseek_base_url,
        model=settings.audit_model,
        timeout=settings.llm_request_timeout_seconds,
        reasoning_effort=settings.audit_reasoning_effort,
        thinking_enabled=settings.audit_thinking_enabled,
    ),
    "audit_bailian": lambda settings: _build_chat_deepseek(
        api_key=settings.bailian_api_key.get_secret_value(),
        base_url=settings.bailian_base_url,
        model=settings.audit_model,
        timeout=settings.llm_request_timeout_seconds,
        reasoning_effort=settings.audit_reasoning_effort,
        thinking_enabled=settings.audit_thinking_enabled,
    ),
    "compression_deepseek": lambda settings: _build_compression_deepseek(settings),
}


def build_provider_llm(provider: str, settings: Any) -> Runnable:
    """Build a single LLM instance for the given provider name.

    Raises:
        ProviderNotRegisteredError: if provider is not in the registry.
    """
    builder = _PROVIDER_REGISTRY.get(provider)
    if builder is None:
        msg = f"Unknown provider '{provider}'. Registered: {list(_PROVIDER_REGISTRY)}"
        raise ProviderNotRegisteredError(msg)
    return builder(settings)


def build_main_llm(settings: Any) -> Runnable[LanguageModelInput, BaseMessage]:
    """Build the main-chat LLM with optional fallback chain.

    When enable_fallback is True (default), returns a RunnableWithFallbacks:
      primary (settings.main_provider) + fallback (settings.fallback_provider).
    When False, returns the primary LLM instance directly (no fallback wrapper).

    Retry policy: 3 attempts with exponential jitter on RateLimitError,
    APITimeoutError, APIConnectionError (applied by with_retry).
    """
    from openai import APIConnectionError, APITimeoutError, RateLimitError

    primary = build_provider_llm(settings.main_provider, settings)

    if not settings.enable_fallback or settings.fallback_provider is None:
        return primary

    secondary = build_provider_llm(settings.fallback_provider, settings)

    retryable = primary.with_retry(
        retry_if_exception_type=(RateLimitError, APITimeoutError, APIConnectionError),
        stop_after_attempt=3,
        wait_exponential_jitter=True,
    )
    return retryable.with_fallbacks([secondary])


