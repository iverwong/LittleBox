"""LLM provider factory: OpenAI-compatible client with fallback chain.

Architecture decision (M6 Step 2.5, 2026-05-02 — out-of-plan deviation):

This factory replaces the plain-class ChatDashScopeQwen approach (Step 0)
with langchain_openai.ChatOpenAI + with_fallbacks. Both DeepSeek and
Aliyun Bailian provide OpenAI-compatible endpoints, so a single client
class with two instances (different base_url + api_key + model) handles
both providers.

Why this iteration overrides baseline §10:
  Original §10 reasoning #2 ("no with_fallbacks consumption") no longer
  holds — M6 now requires 429 auto-fallback DeepSeek -> Bailian. Reasoning
  #1 (built-in tools) and #3 (Qwen-specific thinking_budget / enable_search)
  remain valid in principle but are out of M6 scope.

Trade-offs accepted:
  - Lose DashScope-native: enable_thinking, thinking_budget, enable_search,
    search_options. M6 does not need these (M3 decided to disable thinking
    to reduce first-token latency).
  - Gain: with_fallbacks (1 line), with_retry (1 line), LangSmith trace
    compatibility, with_structured_output (future Pydantic JSON mode),
    standard OpenAI exception model.

Consumer contract (graph nodes / chat stream):
  - get_chat_llm() returns a Runnable with .astream(messages) and
    .ainvoke(messages) yielding AIMessageChunk / AIMessage. The fallback
    chain is opaque to consumers.
"""
from functools import lru_cache

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from openai import APIConnectionError, APITimeoutError, RateLimitError


@lru_cache(maxsize=1)
def get_chat_llm() -> Runnable[LanguageModelInput, BaseMessage]:
    """Build the main-chat LLM singleton: DeepSeek primary + Bailian fallback.

    Retry policy: 3 attempts with exponential jitter on RateLimitError,
    APITimeoutError, APIConnectionError. After retry exhaustion, falls back
    to Bailian (qwen-plus via OpenAI-compatible endpoint).
    """
    from app.config import settings

    deepseek_llm = ChatOpenAI(
        base_url=settings.deepseek_base_url,
        api_key=settings.deepseek_api_key,
        model=settings.deepseek_model,
        timeout=settings.llm_request_timeout_seconds,
    )

    bailian_llm = ChatOpenAI(
        base_url=settings.bailian_base_url,
        api_key=settings.bailian_api_key,
        model=settings.bailian_model,
        timeout=settings.llm_request_timeout_seconds,
    )

    retryable = deepseek_llm.with_retry(
        retry_if_exception_type=(RateLimitError, APITimeoutError, APIConnectionError),
        stop_after_attempt=3,
        wait_exponential_jitter=True,
    )

    return retryable.with_fallbacks([bailian_llm])
