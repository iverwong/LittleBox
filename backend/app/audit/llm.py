"""审查 LLM 装配工厂。

复用 M6 `_PROVIDER_REGISTRY` 的 `audit_deepseek` 条目（在 `app/chat/factory.py` 中注册），
装配 `bind_tools([AppendNote, ReplaceInNotes, AuditOutputSchema], tool_choice="any")`。

详见 D11 决议 + 模板 A v2 关注点 1 修正选 A：
放弃 with_structured_output，三 tool 共存，Step 5 graph 按 tool_call.name 路由。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.runnables import Runnable

from app.chat.factory import build_provider_llm
from app.schemas.audit import AppendNote, AuditOutputSchema, ReplaceInNotes

if TYPE_CHECKING:
    from app.config import Settings


def build_audit_llm(settings: Settings) -> Runnable:
    """构建审查 LLM，绑定三 tool + tool_choice="any"。

    tool_choice="any" 强制模型每帧必须选一个 tool 调用。
    ChatDeepSeek 是否透传此参数由 Step 4 live spike 验证；
    不通过时降级为不传 tool_choice（由 prompt 约束）。
    """
    base = build_provider_llm("audit_deepseek", settings)
    return base.bind_tools(
        [AppendNote, ReplaceInNotes, AuditOutputSchema],
        tool_choice="any",
    )
