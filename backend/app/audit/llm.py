"""审查 LLM 装配工厂。

复用 M6 `_PROVIDER_REGISTRY` 的 `audit_deepseek` 条目（在 `app/chat/factory.py` 中注册），
装配 `bind_tools([AppendNote, ReplaceInNotes, AuditOutputSchema])`。

Spike 验证结论（2026-05-18 v1 → 2026-05-18 v2 校正）：
DeepSeek reasoner 模型协议层不支持任何 tool_choice 枚举值（包括 "required"），
返回 400: "deepseek-reasoner does not support this tool_choice"。
经由三步骤 spike 收敛：A) bind_tools(tool_choice="required") → 400
B) .bind(tool_choice="required") → 400
C) 原生 OpenAI SDK 直连 DeepSeek 端点 → 400。
结论：协议层硬约束不可行。
当前实现：不传 tool_choice（默认 "auto"），由 prompt 约束强制模型每帧选一个 tool。

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
    三步骤 spike 验证（2026-05-18 v2）：DeepSeek reasoner 协议层拒绝 tool_choice
    （"deepseek-reasoner does not support this tool_choice"），方案 C 为唯一可行路径。
    """
    base = build_provider_llm("audit_deepseek", settings)
    return base.bind_tools(
        [AppendNote, ReplaceInNotes, AuditOutputSchema],
    )
