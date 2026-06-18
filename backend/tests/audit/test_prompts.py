"""审查 prompt + tool schema JSON 结构测试。"""
from __future__ import annotations

import pytest
from app.domain.audit.prompts import build_audit_system_prompt
from app.domain.audit.schemas import AppendNote, AuditOutputSchema, ReplaceInNotes
from langchain_core.utils.function_calling import convert_to_openai_function

pytestmark = pytest.mark.audit


class TestAuditSystemPrompt:
    """关键词断言（不使用 STUB_*，final 文本直接匹配）。"""

    @staticmethod
    def _prompt() -> str:
        """构造 child_profile + max_iter 调 build_audit_system_prompt，返回 content 字符串。"""
        from datetime import date
        from app.domain.accounts.schemas import ChildProfileSnapshot

        profile = ChildProfileSnapshot(
            child_user_id="00000000-0000-0000-0000-000000000002",
            nickname="test_kid",
            gender="unknown",
            birth_date=date(2013, 1, 1),
            age=12,
        )
        return build_audit_system_prompt(profile, max_iter=5).content

    def test_contains_role_identity(self):
        prompt = self._prompt()
        assert "独立安全审查员" in prompt
        assert "你不直接与用户对话" in prompt

    def test_contains_tool_usage_protocol(self):
        prompt = self._prompt()
        assert "AppendNote" in prompt
        assert "ReplaceInNotes" in prompt
        assert "AuditOutputSchema" in prompt
        assert "单独调用 AuditOutputSchema" in prompt
        # max_iter 注入工作流程段
        assert "你最多拥有 5 次迭代次数" in prompt

    def test_contains_tool_return_protocol(self):
        prompt = self._prompt()
        # 当前实现:Tool 返回协议是隐式 contract(通过 current_notes 字段返回),
        # 关键词断言转向"# 审查笔记(session_notes)"与"# 引导注入(guidance_injection)"段
        assert "# 审查笔记(session_notes)" in prompt
        assert "AppendNote 和 ReplaceInNotes" in prompt

    def test_contains_signal_guidelines(self):
        prompt = self._prompt()
        assert "# 危机(crisis)" in prompt
        assert "# 红线(redline)" in prompt
        assert "crisis_detected" in prompt


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
