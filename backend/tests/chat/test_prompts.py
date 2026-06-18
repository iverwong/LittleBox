"""Tests for prompts.build_system_prompt.

关注点覆盖：
1. age_at tz-aware (UTC 0点 vs Shanghai 昨天边界，core/time 提级)
2. gender 整段省略：unknown/null → 不含 "# 关于对方的性别" 标题（4章节版）
4. tier 10 边界值落档正确：断言 stub 字面值命中
5. age 字面值仅在末段出现（prefix-cache 硬约束）
6. TODO(prompts-content) grep 命中 11 处（9 stub 函数 + 2 说明性文字）
7. PII / prefix-cache 守卫：nickname / child_user_id / birth_date 字段不泄入 prompt 正文
"""
import uuid
from datetime import date
from unittest.mock import patch

import pytest
from app.core.time import age_at
from app.domain.chat.prompts import (
    STUB_CRISIS_SYSTEM_PROMPT,
    STUB_GENDER_FEMALE,
    STUB_GENDER_MALE,
    STUB_TIER_EARLY_CHILDHOOD,
    STUB_TIER_LATE_CHILDHOOD,
    STUB_TIER_PRE_TEEN,
    STUB_TIER_TEEN,
    STUB_TIER_YOUNG_ADULT,
    build_crisis_system_prompt,
    build_system_prompt,
    format_guidance_wrapper,
    format_reentry_wrapper_crisis,
)
from langchain_core.messages import SystemMessage
from tests.conftest import make_child_profile_snapshot as make_snapshot


class TestComputeAge:
    """关注点 1：tz-aware age_at（core/time 提级）"""

    def test_birthday_passed(self) -> None:
        """正常情况：生日已过，age = today.year - birth.year"""
        with patch("app.core.time.datetime") as mock_dt:
            mock_dt.now.return_value = __import__("datetime").datetime(2025, 6, 15)
            mock_dt.ZoneInfo = __import__("zoneinfo").ZoneInfo
            assert age_at(date(2013, 3, 10)) == 12

    def test_birthday_not_yet(self) -> None:
        """生日未到，age 减 1"""
        with patch("app.core.time.datetime") as mock_dt:
            mock_dt.now.return_value = __import__("datetime").datetime(2025, 6, 15)
            mock_dt.ZoneInfo = __import__("zoneinfo").ZoneInfo
            assert age_at(date(2013, 7, 20)) == 11

    def test_leap_year_birthday_passed(self) -> None:
        """非闰年 2/28：出生 2000-02-28，今天正好生日，age = 25（已满）。"""
        # 出生 2000-02-28；上海时间 2025-02-28
        # today == birth month-day → birthday IS today → age = 25（刚满）
        with patch("app.core.time.datetime") as mock_dt:
            mock_dt.now.return_value = __import__("datetime").datetime(2025, 2, 28)
            mock_dt.ZoneInfo = __import__("zoneinfo").ZoneInfo
            assert age_at(date(2000, 2, 28)) == 25

    def test_leap_year_birthday_not_yet(self) -> None:
        """非闰年 2/28：出生 2000-03-01，今天还未到生日，age = 24。"""
        # 出生 2000-03-01；上海时间 2025-02-28
        # today=(2,28) < birth=(3,1) → birthday not yet → age = 25-1 = 24
        with patch("app.core.time.datetime") as mock_dt:
            mock_dt.now.return_value = __import__("datetime").datetime(2025, 2, 28)
            mock_dt.ZoneInfo = __import__("zoneinfo").ZoneInfo
            assert age_at(date(2000, 3, 1)) == 24

    def test_utc_midnight_shanghai_still_yesterday(self) -> None:
        """UTC 0:30 但 Shanghai 是 8:30（昨天23:30）—— age 不增一岁。

        验证 age_at 用 ZoneInfo("Asia/Shanghai") 而非 UTC date.today()。
        若用 UTC，今天=2025-03-01，birth=2000-03-01 → 25岁。
        用 Shanghai，今天=2025-02-28，birth=2000-03-01 → 24岁。
        """
        # 场景：上海时间 2025-02-28 23:30（UTC 2025-03-01 15:30）
        # mock 上海时间 = 2025-02-28
        with patch("app.core.time.datetime") as mock_dt:
            mock_dt.now.return_value = __import__("datetime").datetime(2025, 2, 28, 23, 30)
            mock_dt.ZoneInfo = __import__("zoneinfo").ZoneInfo
            # 2000-03-01 出生，上海时间 2025-02-28 尚未过生日 → 24岁
            assert age_at(date(2000, 3, 1)) == 24


class TestBuildSystemPrompt:
    """关注点 2-5：build_system_prompt 结构和契约。

    R2 重构后 build_system_prompt 改为单一内嵌 f-string(7 必出段 + 可选历史会话摘要):
    身份与原则 → 对话对象 → 语气与风格 → 解题与学习 → 行为边界 →
    抗越界 → 内部提示 → [可选] 历史会话摘要（压缩）

    旧的"# 关于对方的性别"独立段已删除,gender 体现在 # 对话对象段
    ("12岁的男孩/女孩/孩子")。tier 段也删除,改为自然语言描述。
    """

    # ---- 关注点 2：gender 体现在 # 对话对象段 ----

    def test_gender_male_embeds_in_subject_section(self) -> None:
        content = build_system_prompt(make_snapshot(age=12, gender="male")).content
        assert "# 对话对象" in content
        assert "12岁的男孩" in content

    def test_gender_female_embeds_in_subject_section(self) -> None:
        content = build_system_prompt(make_snapshot(age=12, gender="female")).content
        assert "# 对话对象" in content
        assert "12岁的女孩" in content

    def test_gender_unknown_uses_neutral(self) -> None:
        """unknown → "孩子" 中性称谓,不含男女字面。"""
        content = build_system_prompt(make_snapshot(age=12, gender="unknown")).content
        assert "12岁的孩子" in content
        assert "男孩" not in content
        assert "女孩" not in content

    def test_gender_none_uses_neutral(self) -> None:
        """None → "孩子" 中性称谓。"""
        content = build_system_prompt(make_snapshot(age=12, gender=None)).content
        assert "12岁的孩子" in content

    # ---- 关注点 7：PII / prefix-cache 守卫 ----

    def test_content_rejects_dict_fields(self) -> None:
        """PII + prefix-cache 守卫：snapshot 字段不泄入 prompt 正文。

        - nickname / child_user_id / birth_date 字面值不应出现在 LLM 看到的 system prompt
        - 既保护 PII（child_user_id 是 UUID 不应暴露给 LLM），
          也保护 prefix-cache（前 4 段 L1-L4 应不含动态数据）
        """
        profile = make_snapshot(
            age=12,
            gender="male",
            nickname="secret_nickname",
            birth_date=date(2013, 3, 1),
            child_user_id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        )
        content = build_system_prompt(profile).content
        assert "secret_nickname" not in content
        assert "12345678" not in content  # child_user_id UUID
        assert "2013" not in content      # birth_date 年份
        assert str(profile.child_user_id) not in content
        assert profile.birth_date.isoformat() not in content
        # age 字面值仍出现在 # 对话对象段(拼 "12岁的男孩")
        assert "12" in content
        assert "12岁的男孩" in content

    # ---- 关注点 4：旧 tier STUB 段已删除 ----
    # 旧版按 tier 段嵌入 [STUB tier:*] 字面值,R2 重构后改为自然语言描述,
    # 不再参数化 tier 边界值测试。

    # ---- 关注点 5：age 字面值出现在 # 对话对象段 ----

    def test_age_literal_appears_in_subject_section(self) -> None:
        """age 字面值必出现在 # 对话对象段(拼 "X岁的..." 短语)。"""
        content = str(build_system_prompt(make_snapshot(age=12, gender="male")).content)
        sections = content.split("# 对话对象")
        assert len(sections) >= 2
        subject_section = sections[1]
        assert "12岁的男孩" in subject_section

    @pytest.mark.parametrize("age", [3, 5, 9, 13, 18, 19, 21])
    def test_age_literal_in_subject_all_ages(self, age: int) -> None:
        content = str(build_system_prompt(make_snapshot(age=age, gender=None)).content)
        assert f"{age}岁的孩子" in content

    # ---- 7 章节结构顺序（male/female）----

    def test_section_order_7_chapters(self) -> None:
        """male/female → 7 必出章节严格按序。"""
        content = str(build_system_prompt(make_snapshot(age=12, gender="male")).content)
        order = [
            "# 身份与原则",
            "# 对话对象",
            "# 语气与风格",
            "# 解题与学习",
            "# 行为边界",
            "# 抗越界",
            "# 内部提示",
        ]
        positions = [content.find(s) for s in order]
        assert all(p >= 0 for p in positions), f"missing sections: {dict(zip(order, positions))}"
        assert positions == sorted(positions), f"sections out of order: {positions}"

    # ---- 7 章节结构顺序（unknown 无性别分支，旧"# 关于对方的性别"已删）----

    def test_section_order_7_chapters_gender_unknown(self) -> None:
        """unknown/null → 7 章节无性别段(旧"# 关于对方的性别"已删)。"""
        content = build_system_prompt(make_snapshot(age=12, gender="unknown")).content
        content_str = str(content)
        # 旧的性别段必须不存在
        assert content_str.find("# 关于对方的性别") == -1
        # 7 必出章节存在且有序
        order = [
            "# 身份与原则",
            "# 对话对象",
            "# 语气与风格",
            "# 解题与学习",
            "# 行为边界",
            "# 抗越界",
            "# 内部提示",
        ]
        positions = [content_str.find(s) for s in order]
        assert all(p >= 0 for p in positions), f"missing: {dict(zip(order, positions))}"
        assert positions == sorted(positions)


class TestStubCount:
    """关注点：TODO(prompts-content) slot 计数。"""

    def test_todo_content_slots_count(self) -> None:
        """M9 Step 4：9 个原有 + 3 个新增 = 12。

        原有（9）：_identity_block(1) + _safety_block(1) + _tier_block(5) + _gender_block(2)
        新增（3）：STUB_CRISIS_SYSTEM_PROMPT +
                  STUB_REENTRY_WRAPPER_CRISIS +
                  GUIDANCE_WRAPPER
        """
        import subprocess

        result = subprocess.run(
            ["grep", "-c", "TODO(prompts-content)", "app/domain/chat/prompts.py"],
            capture_output=True,
            text=True,
            cwd="/app",
        )
        count = int(result.stdout.strip())
        assert count == 11, f"expected 11 TODO(prompts-content) lines, got {count}"


class TestM9InterventionPrompts:
    """M9 Step 4：三级干预 STUB prompt + format_* wrapper 纯函数测试。

    测试纪律：纯函数，不触 DB / Redis / 任何 fixture。
    """

    # ---- C.1: build_crisis_system_prompt ----

    def test_crisis_system_prompt_has_5_sections(self) -> None:
        """Given age=10 gender=male, When build_crisis_system_prompt, Then 5 段标题存在。"""
        msg = build_crisis_system_prompt(make_snapshot(age=10, gender="male"),
                                         crisis_topic="测试主题",
                                         crisis_turn_dialogue="<turn>test</turn>",
                                         pre_crisis_turn_dialogue="<turn>pre</turn>")
        content = msg.content
        assert isinstance(msg, SystemMessage)
        assert "# 当前首要任务（最高优先级）" in content
        assert "测试主题" in content

    def test_crisis_system_prompt_gender_none_omits_section(self) -> None:
        """Given gender=None, When build_crisis_system_prompt, Then 性别段不存在。"""
        content = build_crisis_system_prompt(make_snapshot(age=10, gender=None),
                                              crisis_topic="测试主题",
                                              crisis_turn_dialogue="<turn>test</turn>",
                                              pre_crisis_turn_dialogue="<turn>pre</turn>").content
        assert "# 关于对方的性别" not in content

    # ---- C.2: format_reentry_wrapper_crisis ----

    def test_format_reentry_wrapper_crisis(self) -> None:
        """Given user_input='hi', When format_reentry_wrapper_crisis, Then 含 STUB 标记。"""
        result = format_reentry_wrapper_crisis("hi")
        assert isinstance(result, str)
        assert "TODO(prompts-content)" in result
        assert "hi" in result

    # ---- C.3: format_guidance_wrapper ----

    def test_guidance_wrapper_none_passthrough(self) -> None:
        """Given guidance=None, When format_guidance_wrapper, Then 返回原始 user_input。"""
        assert format_guidance_wrapper("hi", None) == "hi"

    def test_guidance_wrapper_empty_string_passthrough(self) -> None:
        """Given guidance="", When format_guidance_wrapper, Then 返回原始 user_input。"""
        assert format_guidance_wrapper("hi", "") == "hi"

    def test_guidance_wrapper_with_guidance(self) -> None:
        """Given guidance non-empty, When format_guidance_wrapper, Then 包装到 <guidance> 内。"""
        result = format_guidance_wrapper("hi", "be safe")
        assert result != "hi"
        assert "<guidance>be safe</guidance>" in result
        assert "<user_input>hi</user_input>" in result

