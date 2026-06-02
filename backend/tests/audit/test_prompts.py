"""审查 prompt + tool schema JSON 结构测试。"""
from __future__ import annotations

import json

import pytest
from langchain_core.utils.function_calling import convert_to_openai_function

from app.audit.prompts import build_audit_system_prompt
from app.schemas.audit import AppendNote, AuditOutputSchema, ReplaceInNotes

pytestmark = pytest.mark.audit


class TestAuditSystemPrompt:
    """关键词断言（不使用 STUB_*，final 文本直接匹配）。"""

    def test_contains_role_identity(self):
        prompt = build_audit_system_prompt()
        assert "审查 Agent" in prompt
        assert "你**不直接与子账号对话**" in prompt

    def test_contains_tool_usage_protocol(self):
        prompt = build_audit_system_prompt()
        assert "AppendNote" in prompt
        assert "ReplaceInNotes" in prompt
        assert "AuditOutputSchema" in prompt
        assert "tool_choice" in prompt or "必须选一个 tool" in prompt
        assert "唯一匹配" in prompt

    def test_contains_tool_return_protocol(self):
        prompt = build_audit_system_prompt()
        assert "Tool 返回协议" in prompt
        assert "current_notes" in prompt
        assert "脑补" in prompt

    def test_contains_signal_guidelines(self):
        prompt = build_audit_system_prompt()
        assert "危机信号" in prompt
        assert "红线信号" in prompt
        assert "crisis_detected" in prompt
        assert "redline_triggered" in prompt


class TestToolSchemas:
    """bind_tools 前后 JSON schema 结构正确。"""

    def test_append_note_json_schema(self):
        schema = convert_to_openai_function(AppendNote)
        props = schema["parameters"]["properties"]
        assert "text" in props
        assert schema["parameters"]["required"] == ["text"]
        assert props["text"]["maxLength"] == 500

    def test_replace_in_notes_json_schema(self):
        schema = convert_to_openai_function(ReplaceInNotes)
        props = schema["parameters"]["properties"]
        assert "old_str" in props
        assert "new_str" in props
        assert sorted(schema["parameters"]["required"]) == sorted(["old_str", "new_str"])

    def test_audit_output_schema_json_schema(self):
        schema = convert_to_openai_function(AuditOutputSchema)
        props = schema["parameters"]["properties"]
        assert "dimension_scores" in props
        assert "crisis_detected" in props
        assert "guidance_injection" in props
        # 字段类型为 str | None，pydantic 用 anyOf 表达；maxLength 嵌在 string 子项
        string_branch = next(
            b for b in props["guidance_injection"]["anyOf"]
            if b.get("type") == "string"
        )
        assert string_branch["maxLength"] == 300
