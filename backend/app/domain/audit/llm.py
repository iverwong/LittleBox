"""审查 LLM 装配工厂（D11 v3）。

复用 M6 `_PROVIDER_REGISTRY` 的 `audit_deepseek`（主）和 `audit_bailian`（备）条目，
装配 `bind_tools([AppendNote, ReplaceInNotes, AuditOutputSchema])`，不传 tool_choice。

D11 v3（M8-hotfix）：确认 DS/BL 两端思考模式均不支持 tool_choice="required" 或 "any"
（36 变体穷尽实证），统一走 "auto" + system prompt 强约束 + post-processing 兜底。

M8-hotfix 追加 with_retry / with_fallbacks 包装：
- 主端 3 次重试（瞬态错误自动恢复）
- 备端百炼（主端全量失败时 fallback）
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.runnables import Runnable

from app.core.llm import build_provider_llm
from app.domain.audit.schemas import AppendNote, AuditOutputSchema, ReplaceInNotes

if TYPE_CHECKING:
    from app.core.config import Settings


def build_audit_llm(settings: Settings) -> Runnable:
    """构建审查 LLM：主端（deepseek）+ retry + fallback（百炼），最后 bind_tools。

    Retry 策略：3 次指数退避，捕获 RateLimitError / APITimeoutError / APIConnectionError。
    Fallback 策略：主端 3 次重试全失败后，切到百炼备端。
    """
    from openai import APIConnectionError, APITimeoutError, RateLimitError

    primary = build_provider_llm("audit_deepseek", settings)
    secondary = build_provider_llm("audit_bailian", settings)

    # 先 bind_tools 再包 retry/fallback，确保工具绑定在各层都生效
    primary_bound = primary.bind_tools(  # type: ignore[attr-defined]
        [AppendNote, ReplaceInNotes, AuditOutputSchema],
    )
    secondary_bound = secondary.bind_tools(  # type: ignore[attr-defined]
        [AppendNote, ReplaceInNotes, AuditOutputSchema],
    )

    retryable = primary_bound.with_retry(
        retry_if_exception_type=(RateLimitError, APITimeoutError, APIConnectionError),
        stop_after_attempt=3,
        wait_exponential_jitter=True,
    )
    return retryable.with_fallbacks([secondary_bound])
