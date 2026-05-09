"""Provider-aware extractors for finish_reason and reasoning_content.

M6 patch 2 (Step 11.2): replaces inline field-path parsing in graph.py
call_main_llm with dispatch-by-provider helpers. Each provider maps to
its canonical field path for finish_reason and reasoning_content.

Provider field path reference (verified from source 2026-05-09):
  deepseek / openai:
    finish_reason → chunk.additional_kwargs.response_metadata.finish_reason
    (identical to ChatOpenAI; ChatDeepSeek inherits BaseChatOpenAI)
  deepseek:
    reasoning_content → chunk.additional_kwargs.reasoning_content
    (ChatDeepSeek extracts it in _convert_chunk_to_generation_chunk,
     langchain_deepseek/chat_models.py:309-314)
  openai:
    reasoning_content → None  (ChatOpenAI drops third-party reasoning fields)
"""

from langchain_core.messages import AIMessageChunk

ALLOWED_FINISH_REASONS = frozenset({"stop", "length", "content_filter"})

# Provider → field path for finish_reason extraction
_FINISH_REASON_PATH: dict[str, str] = {
    "deepseek": "response_metadata.finish_reason",
    "openai": "response_metadata.finish_reason",
}


def extract_finish_reason(chunk: AIMessageChunk, provider: str) -> str | None:
    """Extract finish_reason by provider. None if absent or not in whitelist.

    白名单（ALLOWED_FINISH_REASONS）：
      stop / length / content_filter — 透传
      tool_calls / 其他 — 丢弃（返回 None）

    Args:
        chunk: LLM 输出 chunk
        provider: provider 名（deepseek / openai）；未注册的 provider 走 deepseek 路径

    Returns:
        白名单内的 finish_reason 值，或 None
    """
    ak = chunk.additional_kwargs or {}
    metadata = ak.get("response_metadata") or {}
    fr = metadata.get("finish_reason")
    return fr if fr in ALLOWED_FINISH_REASONS else None


def extract_reasoning_content(chunk: AIMessageChunk, provider: str) -> str | None:
    """Extract reasoning content (thinking text) by provider. None if absent.

    Args:
        chunk: LLM 输出 chunk
        provider: provider 名
          - deepseek: 走 additional_kwargs.reasoning_content（ChatDeepSeek 已提取）
          - openai: 恒返回 None（ChatOpenAI 丢弃第三方 reasoning 字段）

    Returns:
        reasoning_content 字符串，或 None
    """
    if provider == "deepseek":
        ak = chunk.additional_kwargs or {}
        return ak.get("reasoning_content")
    return None
