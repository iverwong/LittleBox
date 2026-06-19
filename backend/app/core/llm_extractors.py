"""按模型档解耦的 chunk 字段提取器。

`extract_finish_reason` 与 provider 无关（白名单透传路径两 provider
早已统一）；`extract_reasoning_content` 改由 `ModelProfile.supports_reasoning`
判定，非推理族翻 `False` 即可关闭提取。

字段路径依据（langchain 端实测）：
- `finish_reason` → `chunk.response_metadata["finish_reason"]`（直接属性，
  非 `additional_kwargs["response_metadata"]` 内嵌；末 5 chunk 中
  `additional_kwargs` 恒为 `{}`）；
- `reasoning_content` → `chunk.additional_kwargs["reasoning_content"]`，
  `ModelProfile.supports_reasoning=False` 时直接返回 `None`。
"""

from langchain_core.messages import AIMessageChunk

from app.core.llm_topology import ROLES, ModelProfile, Role, resolve_profile

ALLOWED_FINISH_REASONS = frozenset({"stop", "length", "content_filter"})
"""`extract_finish_reason` 接受的 finish_reason 白名单。其他值（含 `tool_calls`）一律丢弃。"""


def role_profile(role: Role) -> ModelProfile:
    """role → `ModelProfile` 解析。

    即 `resolve_profile(ROLES[role].model)` 的语义化封装，让 extractor
    无需直接耦合 `ROLES` / `resolve_profile` 两个名字。

    Returns:
        对应 role 的 `ModelProfile`。

    Raises:
        ModelProfileNotRegisteredError: `ROLES[role].model` 无对应模型档。
    """
    return resolve_profile(ROLES[role].model)


def extract_finish_reason(chunk: AIMessageChunk) -> str | None:
    """从 `chunk.response_metadata` 提取白名单内的 finish_reason。

    白名单（`ALLOWED_FINISH_REASONS`）：`stop` / `length` / `content_filter`
    透传；`tool_calls` 与其他值丢弃（返回 `None`）。

    Args:
        chunk: LLM 输出 chunk。

    Returns:
        白名单内的 finish_reason 值，或 `None`。
    """
    metadata = chunk.response_metadata or {}
    fr = metadata.get("finish_reason")
    return fr if fr in ALLOWED_FINISH_REASONS else None


def extract_usage(chunk: AIMessageChunk) -> dict | None:
    """从 LLM 末帧提取 usage 元数据。

    实际字段路径：`chunk.usage_metadata`（`AIMessageChunk` 标准字段，
    末帧由 SDK 自动设置）。

    Args:
        chunk: LLM 输出 chunk。

    Returns:
        `{"input_tokens", "output_tokens", "total_tokens"}` 字典，或
        `None`（usage 不可用时）。
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
    """按模型档提取 reasoning_content（思考文本），无则返回 `None`。

    模型档判定取代了 provider 字符串判定：
    - `profile.supports_reasoning=True`：走
      `chunk.additional_kwargs["reasoning_content"]`；
    - `profile.supports_reasoning=False`：直接返回 `None`。

    Args:
        chunk: LLM 输出 chunk。
        profile: 模型档，决定是否走 reasoning 提取路径。

    Returns:
        reasoning_content 字符串，或 `None`（不支持 / 字段缺失）。
    """
    if not profile.supports_reasoning:
        return None
    ak = chunk.additional_kwargs or {}
    return ak.get("reasoning_content")
