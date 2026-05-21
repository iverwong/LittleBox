"""Provider-aware extractors for finish_reason and reasoning_content.

M8-hotfix (Step 1): finish_reason 取值路径修正。
修正依据见 LLM Provider 探针 F1 实证：
  - `chunk.additional_kwargs` 在末 chunk 恒为 {}，取值恒 None
  - 真路径为 `chunk.response_metadata["finish_reason"]`（LangChain 标准属性）

Provider field path reference (verified from probe 2026-05-19):
  deepseek / openai:
    finish_reason → chunk.response_metadata["finish_reason"]
    (直接属性，非 additional_kwargs 内嵌)
  deepseek:
    reasoning_content → chunk.additional_kwargs.reasoning_content
    (ChatDeepSeek 在 _convert_chunk_to_generation_chunk 中提取)
  openai:
    reasoning_content → None  (ChatOpenAI 丢弃第三方 reasoning 字段)
"""

from langchain_core.messages import AIMessageChunk

ALLOWED_FINISH_REASONS = frozenset({"stop", "length", "content_filter"})

# Provider → field path for finish_reason extraction
_FINISH_REASON_PATH: dict[str, str] = {
    "deepseek": "response_metadata.finish_reason",
    "openai": "response_metadata.finish_reason",
}


def extract_finish_reason(chunk: AIMessageChunk, provider: str) -> str | None:
    """从 chunk.response_metadata 提取 finish_reason。

    M8-hotfix 修正：真路径是 chunk.response_metadata["finish_reason"] 直接属性，
    而非 additional_kwargs["response_metadata"]["finish_reason"]。
    探针实证末 5 chunk 中 additional_kwargs 恒为 {}。

    白名单（ALLOWED_FINISH_REASONS）：
      stop / length / content_filter — 透传
      tool_calls / 其他 — 丢弃（返回 None）

    Args:
        chunk: LLM 输出 chunk
        provider: provider 名（仅用于签名兼容，两 provider 路径已一致）

    Returns:
        白名单内的 finish_reason 值，或 None
    """
    metadata = chunk.response_metadata or {}
    fr = metadata.get("finish_reason")
    return fr if fr in ALLOWED_FINISH_REASONS else None


def extract_usage(chunk: AIMessageChunk) -> dict | None:
    """从 LLM 末帧提取 usage 元数据。

    真路径（已验证 langchain_openai BaseChatOpenAI._convert_chunk_to_generation_chunk）：
      chunk.usage_metadata（AIMessageChunk 标准字段，末帧由 SDK 自动设置）

    Returns:
        {"input_tokens": int, "output_tokens": int, "total_tokens": int}
        或 None（usage 不可用时）
    """
    um = chunk.usage_metadata
    if um is None:
        return None
    return {
        "input_tokens": um.get("input_tokens", 0) if isinstance(um, dict) else um.input_tokens,
        "output_tokens": um.get("output_tokens", 0) if isinstance(um, dict) else um.output_tokens,
        "total_tokens": um.get("total_tokens", 0) if isinstance(um, dict) else um.total_tokens,
    }


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
