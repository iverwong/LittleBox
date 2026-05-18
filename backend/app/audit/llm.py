"""审查 LLM 装配工厂。

复用 M6 `_PROVIDER_REGISTRY` 的 `audit_deepseek` 条目（在 `app/chat/factory.py` 中注册），
装配 `bind_tools([AppendNote, ReplaceInNotes, AuditOutputSchema])`。

Spike 验证结论（2026-05-18）：DeepSeek reasoner 类模型不支持 ``tool_choice="any"``
（OpenAI ``tool_choice="required"``），降级为不传 tool_choice（默认 "auto"），
由 prompt 约束（《# 输出方式》节）强制模型每帧选一个 tool。

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
    """构建审查 LLM，绑定三 tool。

    不传 tool_choice（默认 "auto"），由 prompt 约束强制模型每帧选择一个工具。
    """
    base = build_provider_llm("audit_deepseek", settings)
    return base.bind_tools(
        [AppendNote, ReplaceInNotes, AuditOutputSchema],
    )
