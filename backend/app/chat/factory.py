"""LLM provider factory: provider registry + ChatDeepSeek primary + fallback chain.

M6 patch 2 (Step 11.1): replaces the ChatOpenAI-only factory with a
_PROVIDER_REGISTRY dispatching by settings.main_provider. ChatDeepSeek
is the primary provider (preserving reasoning_content); ChatOpenAI is
registered for future M11+ experiments but not used in M6.
"""

from collections.abc import Callable
from functools import lru_cache
from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI


class ProviderNotRegisteredError(LookupError):
    """Provider 名未在 _PROVIDER_REGISTRY 中注册时抛出。"""


def _build_chat_deepseek(
    api_key: str,
    base_url: str,
    model: str,
    timeout: float,
    reasoning_effort: str,
) -> ChatDeepSeek:
    """Construct ChatDeepSeek with thinking mode enabled."""
    return ChatDeepSeek(
        api_key=api_key,  # type: ignore[arg-type]
        base_url=base_url,
        model=model,
        timeout=timeout,
        extra_body={
            "thinking": {"type": "enabled"},
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
    )


_PROVIDER_REGISTRY: dict[str, Callable[..., Runnable]] = {
    "deepseek": lambda settings: _build_chat_deepseek(
        api_key=settings.deepseek_api_key.get_secret_value(),
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout=settings.llm_request_timeout_seconds,
        reasoning_effort=settings.deepseek_reasoning_effort,
    ),
    "openai": lambda settings: _build_chat_openai(
        api_key=settings.bailian_api_key.get_secret_value(),
        base_url=settings.bailian_base_url,
        model=settings.bailian_model,
        timeout=settings.llm_request_timeout_seconds,
    ),
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


# ---- backward compat (M6 Step 2.5 API) — remove after graph.py migration (Step 11.2) ----


@lru_cache(maxsize=1)
def get_chat_llm() -> Runnable[LanguageModelInput, BaseMessage]:
    """返回主对话 LLM 单例（已弃用，Step 11.2 后由 graph.py 直接调用 build_main_llm）。

    Deprecated: M6 Step 2.5 compat layer. graph.py call_main_llm 仍引用此函数，
    Step 11.2 迁移后删除。
    """
    from app.config import settings

    return build_main_llm(settings)
