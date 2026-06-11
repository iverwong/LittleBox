"""审查 LLM 装配工厂（Step 6 重构后）。

通过 `app.core.llm` 的 role 驱动入口装配：

- 主端：`build_role_primary(Role.AUDIT, settings)` → deepseek-v4 + thinking +
  reasoning_effort=MAX（裸 `BaseChatModel`）
- 备端：`build_role_fallback(Role.AUDIT, settings)` → bailian + thinking +
  reasoning_effort=MAX（裸 `BaseChatModel`）
- 主备各 `bind_tools([AppendNote, ReplaceInNotes, AuditOutputSchema])`，
  再 `wrap_resilience(..., retry_attempts=ROLES[Role.AUDIT].retry_attempts)`
  主端重试 + 备端兜底一气呵成
- 不传 `tool_choice`（D11 v3 实证：DS/BL 两端思考模式均不支持
  `tool_choice="required"` 或 `"any"`，统一走 `"auto"` + system prompt 强约束
  + post-processing 兜底）

行为变化（Step 3 引入、本步重写实现）：

- `retry_attempts` 显式读 `ROLES[Role.AUDIT].retry_attempts`（今日 = 3）单一真相源，
  改 `ROLES` 即跟随——避免走 `wrap_resilience` 默认值 3 静默脱钩
- 不再走 `build_provider_llm` 字符串拼接（Step 6 同步移除唯一生产调用方）
- shim `build_provider_llm` 在 `app/core/llm.py` 保留到 Step 7（因 30+ 测试用例
  与集成测试注入缝仍在引用，Step 7 整体收口时删）
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.runnables import Runnable

from app.core.llm import build_role_fallback, build_role_primary, wrap_resilience
from app.core.llm_topology import ROLES, Role
from app.domain.audit.schemas import AppendNote, AuditOutputSchema, ReplaceInNotes

if TYPE_CHECKING:
    from app.core.config import Settings


def build_audit_llm(settings: Settings) -> Runnable:
    """构建审查 LLM（Step 6 重构后）：主备裸实例各 bind_tools，再 wrap_resilience 一体化。

    装配链：

        1. `build_role_primary(Role.AUDIT, settings)` → 主端裸 ChatDeepSeek（deepseek-v4）
        2. `build_role_fallback(Role.AUDIT, settings)` → 备端裸 ChatDeepSeek（bailian）
        3. 主备各 `bind_tools([AppendNote, ReplaceInNotes, AuditOutputSchema])`
        4. `wrap_resilience(primary_bound, fallback_bound, retry_attempts=
           ROLES[Role.AUDIT].retry_attempts)` → `with_retry(stop=3) + with_fallbacks`

    Retry 策略：`stop_after_attempt=ROLES[Role.AUDIT].retry_attempts`（今日 = 3），
    指数抖动，捕获 `RateLimitError` / `APITimeoutError` / `APIConnectionError`。
    Fallback 策略：主端 retry 耗尽后切到百炼备端（思考 + 不绑工具能力等价）。

    Raises:
        RuntimeError: 防御性 — 若 `ROLES[Role.AUDIT].fallback` 被配成 None，
            显式抛错而非静默退化。生产拓扑今日总是带 bailian fallback。
    """
    primary = build_role_primary(Role.AUDIT, settings)
    fallback = build_role_fallback(Role.AUDIT, settings)
    if fallback is None:
        # 防御性：plan 关注点 1 要求基于 None 分支显式处理
        # 走裸 _build_role_llm 也会因 ROLES[AUDIT].fallback=None 退化为无 fallback,
        # 显式抛错让 topology 改错立即被截获,不让 audit 静默退化为「裸单端实例」
        raise RuntimeError(
            "audit role 必须有 fallback 配置(百炼兜底);"
            "若有意移除请同步改 app/domain/audit/llm.py::build_audit_llm"
        )
    tools = [AppendNote, ReplaceInNotes, AuditOutputSchema]
    primary_bound = primary.bind_tools(  # type: ignore[attr-defined]
        tools,
    )
    fallback_bound = fallback.bind_tools(  # type: ignore[attr-defined]
        tools,
    )
    return wrap_resilience(
        primary_bound,
        fallback_bound,
        retry_attempts=ROLES[Role.AUDIT].retry_attempts,
    )
