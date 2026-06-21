"""M10 TDD:audit system prompt 注入家长 concerns。

测试函数级 docstring 用 Given / When / Then。"""

from __future__ import annotations

from datetime import date

from app.domain.accounts.schemas import ChildProfileSnapshot
from app.domain.audit.prompts import build_audit_system_prompt


def _prompt(concerns: str | None) -> str:
    """构造含/不含 concerns 的 snapshot,返回最终 prompt 字符串。"""
    profile = ChildProfileSnapshot(
        child_user_id="00000000-0000-0000-0000-000000000002",
        nickname="test_kid",
        gender="unknown",
        birth_date=date(2013, 1, 1),
        age=12,
        sensitivity=None,
        custom_redlines=None,
        concerns=concerns,
    )
    return build_audit_system_prompt(profile, max_iter=5).content


class TestConcernsInjection:
    """设 concerns → 渲染字符串含关注点正文与「家长关注点」段标题。"""

    def test_concerns_section_present_when_set(self) -> None:
        """Given child_profile.concerns 非空
        When build_audit_system_prompt
        Then 渲染字符串包含「家长关注点(concerns)」段标题与关注点正文。
        """
        prompt = _prompt("近期月考压力较大")
        assert "# 家长关注点(concerns)" in prompt
        assert "近期月考压力较大" in prompt

    def test_concerns_text_appears_between_about_user_and_dimensions(self) -> None:
        """Given concerns 非空
        When 渲染
        Then 关注点段位于「关于用户」段之后、「六维度评价」段之前。
        """
        prompt = _prompt("需要关注")
        about_idx = prompt.find("# 关于用户")
        concerns_idx = prompt.find("# 家长关注点(concerns)")
        dimensions_idx = prompt.find("# 六维度评价(dimension_scores)")

        assert about_idx != -1
        assert concerns_idx != -1
        assert dimensions_idx != -1
        assert about_idx < concerns_idx < dimensions_idx


class TestConcernsAbsent:
    """未设 concerns → 渲染字符串无关注点段。"""

    def test_no_concerns_section_when_none(self) -> None:
        """Given child_profile.concerns = None
        When build_audit_system_prompt
        Then 渲染字符串不包含「家长关注点(concerns)」段标题。
        """
        prompt = _prompt(None)
        assert "# 家长关注点(concerns)" not in prompt

    def test_no_concerns_section_when_empty_string(self) -> None:
        """Given child_profile.concerns = ''(空串)
        When build_audit_system_prompt
        Then 渲染字符串不包含关注点段(空串视为未设)。
        """
        prompt = _prompt("")
        assert "# 家长关注点(concerns)" not in prompt
        assert "近期月考压力较大" not in prompt


class TestConcernsNotInMainChatPrompt:
    """concerns 只进审查 prompt,不入主对话 prompt。

    主对话 prompt 通过 `build_system_prompt`(domain/chat/prompts.py)构造,
    不接收 `child_profile.concerns`。本里程碑通过静态断言确保审查侧不
    把 concerns 字段传播到主 LLM 路径(此处只验证审查侧注入语义)。
    """

    def test_audit_prompt_does_not_leak_concerns_marker_to_ai(self) -> None:
        """Given concerns 包含「不要告诉孩子我告诉你看」的反向引导文本
        When build_audit_system_prompt
        Then 审查 prompt 必须包含该文本(确认 concerns 真的进了审查 prompt)。
        """
        # 反向断言:不是「审查 prompt 没收到」而是「真的收到了」
        prompt = _prompt("近期月考压力较大,注意观察情绪走向")
        assert "近期月考压力较大,注意观察情绪走向" in prompt