"""按模型档解耦的 finish_reason / reasoning_content 提取器。

Step 5 重构：消除 provider 字符串入参，改由 `ModelProfile` 字段判定
行为差异。`extract_finish_reason` 与 provider 完全无关（白名单透传路径
两 provider 早已统一），`extract_reasoning_content` 改由
`ModelProfile.supports_reasoning` 判定——今日 deepseek-v4 档
`supports_reasoning=True`，未来加非推理族时翻 False 即可。

字段路径参考（M8-hotfix 探针 2026-05-19 实证）：
  - finish_reason → `chunk.response_metadata["finish_reason"]`
    （直接属性，非 `additional_kwargs["response_metadata"]` 内嵌；
    探针实证末 5 chunk 中 `additional_kwargs` 恒为 {}）
  - reasoning_content → `chunk.additional_kwargs["reasoning_content"]`
    （ChatDeepSeek 在 `_convert_chunk_to_generation_chunk` 中提取；
    非推理档 `ModelProfile.supports_reasoning=False` 时直接返回 None）
"""

from langchain_core.messages import AIMessageChunk

from app.core.llm_topology import ROLES, ModelProfile, Role, resolve_profile

ALLOWED_FINISH_REASONS = frozenset({"stop", "length", "content_filter"})


def role_profile(role: Role) -> ModelProfile:
    """role → ModelProfile 解析。

    即 `resolve_profile(ROLES[role].model)` 的语义化封装，
    让 `extract_reasoning_content` 等 extractor 无需直接耦合
    `ROLES` / `resolve_profile` 两个名字（仍依赖 `ModelProfile` 注解，
    该依赖由 `extract_reasoning_content` 签名强制带入，与本函数无关）。

    Raises:
        ModelProfileNotRegisteredError: `ROLES[role].model` 无对应模型档。
    """
    return resolve_profile(ROLES[role].model)


def extract_finish_reason(chunk: AIMessageChunk) -> str | None:
    """从 chunk.response_metadata 提取白名单内的 finish_reason。

    白名单（ALLOWED_FINISH_REASONS）：
      stop / length / content_filter — 透传
      tool_calls / 其他 — 丢弃（返回 None）

    Args:
        chunk: LLM 输出 chunk

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
        "input_tokens": um["input_tokens"],
        "output_tokens": um["output_tokens"],
        "total_tokens": um["total_tokens"],
    }


def extract_reasoning_content(chunk: AIMessageChunk, profile: ModelProfile) -> str | None:
    """按模型档提取 reasoning_content（思考文本），无则返回 None。

    模型档判定取代旧 provider 字符串判定：
      - `profile.supports_reasoning=True`（如 deepseek-v4）：走
        `chunk.additional_kwargs["reasoning_content"]`（ChatDeepSeek
        在 `_convert_chunk_to_generation_chunk` 中提取）
      - `profile.supports_reasoning=False`（如未来纯非推理族）：直接 None

    Args:
        chunk: LLM 输出 chunk
        profile: 模型档（决定是否走 reasoning 提取路径）

    Returns:
        reasoning_content 字符串，或 None（不支持 / 字段缺失）
    """
    if not profile.supports_reasoning:
        return None
    ak = chunk.additional_kwargs or {}
    return ak.get("reasoning_content")
