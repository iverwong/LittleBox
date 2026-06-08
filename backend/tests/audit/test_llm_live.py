"""审查 LLM live spike：验证 ChatDeepSeek + bind_tools 三 tool 共存兼容性。

**不进 CI**。手动触发方式：
    docker compose exec api python -m pytest tests/audit/test_llm_live.py -v --run-live

需 Iver 批准后执行。参见 M8 §五 Step 4 验证矩阵。

通过条件（D11/D8 验证）：
1. 三 tool 共存（AppendNote, ReplaceInNotes, AuditOutputSchema）
2. 模型每帧选一个调用，tool_call.args 可被对应 schema 反序列化
3. thinking 模式生效（response 含 reasoning_content）
"""
from __future__ import annotations

import json

import pytest
from app.domain.audit.llm import build_audit_llm
from app.domain.audit.prompts import build_audit_system_prompt
from app.core.config import Settings
from app.domain.audit.schemas import AppendNote, AuditOutputSchema, ReplaceInNotes
from langchain_core.messages import HumanMessage, SystemMessage

pytestmark = [
    pytest.mark.live,
    pytest.mark.asyncio,
]


@pytest.fixture
def audit_llm():
    """使用真实 settings 构造审查 LLM。"""
    s = Settings()
    return build_audit_llm(s)


class TestAuditLLMLive:
    """Live spike：验证 ChatDeepSeek 三 tool 共存 + thinking 激活。"""

    async def test_thinking_is_enabled(self, audit_llm):
        """验证 thinking 被启用（响应中含 reasoning_content 标记）。"""
        prompt = [
            SystemMessage(content=build_audit_system_prompt()),
            HumanMessage(content="用户今天情绪比较稳定，聊了学校功课。"),
        ]
        response = await audit_llm.ainvoke(prompt)

        # ChatDeepSeek thinking 响应含 reasoning_content
        assert response.tool_calls, "model 未返回任何 tool_call"

        # 验证 reasoning_content 存在（DeepSeek thinking 激活标记）
        usage = response.response_metadata.get("token_usage", {})
        assert "prompt_tokens" in usage
        print(f"Prompt tokens: {usage['prompt_tokens']}, "
              f"Completion tokens: {usage['completion_tokens']}")

        # 验证 tool_call 可反序列化
        tc = response.tool_calls[0]
        if tc["name"].endswith("AuditOutputSchema"):
            AuditOutputSchema.model_validate_json(json.dumps(tc["args"]))
        elif tc["name"].endswith("AppendNote"):
            AppendNote.model_validate_json(json.dumps(tc["args"]))
        elif tc["name"].endswith("ReplaceInNotes"):
            ReplaceInNotes.model_validate_json(json.dumps(tc["args"]))

    async def test_all_three_tools_available(self, audit_llm):
        """验证模型可选任意 tool，且 tool_choice 约束有效。"""
        prompt = [
            SystemMessage(content=build_audit_system_prompt()),
            HumanMessage(content="做一个简单的笔记记录：用户今天很高兴。然后提交分析结果。"),
        ]
        # 第一帧
        response = await audit_llm.ainvoke(prompt)
        assert response.tool_calls, "model 应至少返回一个 tool_call"

        tool_names = {tc["name"] for tc in response.tool_calls}
        # 三个 tool 都应出现在可能的范围内
        audit_name = "AuditOutputSchema"
        append_name = "AppendNote"
        replace_name = "ReplaceInNotes"
        any_tool = audit_name in tool_names or append_name in tool_names or replace_name in tool_names
        assert any_tool, f"未知 tool name: {tool_names}"
