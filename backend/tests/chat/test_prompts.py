"""Tests for prompts.build_system_prompt.

关注点覆盖：
1. compute_age tz-aware (UTC 0点 vs Shanghai 昨天边界)
2. gender 整段省略：unknown/null → 不含 "# 关于对方的性别" 标题（4章节版）
3. 签名级字段消费拒绝：传入多余字段 → TypeError（运行时）
4. tier 10 边界值落档正确：断言 stub 字面值命中
5. age 字面值仅在末段出现（prefix-cache 硬约束）
6. TODO(prompts-content) grep 命中 11 处（9 stub 函数 + 2 说明性文字）
"""
from datetime import date
from unittest.mock import patch

import pytest

from app.chat.prompts import (
    STUB_GENDER_FEMALE,
    STUB_GENDER_MALE,
    STUB_TIER_EARLY_CHILDHOOD,
    STUB_TIER_LATE_CHILDHOOD,
    STUB_TIER_PRE_TEEN,
    STUB_TIER_TEEN,
    STUB_TIER_YOUNG_ADULT,
    build_system_prompt,
    compute_age,
)


class TestComputeAge:
    """关注点 1：tz-aware compute_age"""

    def test_birthday_passed(self) -> None:
        """正常情况：生日已过，age = today.year - birth.year"""
        with patch("app.chat.prompts.datetime") as mock_dt:
            mock_dt.now.return_value = __import__("datetime").datetime(2025, 6, 15)
            mock_dt.ZoneInfo = __import__("zoneinfo").ZoneInfo
            assert compute_age(date(2013, 3, 10)) == 12

    def test_birthday_not_yet(self) -> None:
        """生日未到，age 减 1"""
        with patch("app.chat.prompts.datetime") as mock_dt:
            mock_dt.now.return_value = __import__("datetime").datetime(2025, 6, 15)
            mock_dt.ZoneInfo = __import__("zoneinfo").ZoneInfo
            assert compute_age(date(2013, 7, 20)) == 11

    def test_leap_year_birthday_passed(self) -> None:
        """非闰年 2/28：出生 2000-02-28，今天正好生日，age = 25（已满）。"""
        # 出生 2000-02-28；上海时间 2025-02-28
        # today == birth month-day → birthday IS today → age = 25（刚满）
        with patch("app.chat.prompts.datetime") as mock_dt:
            mock_dt.now.return_value = __import__("datetime").datetime(2025, 2, 28)
            mock_dt.ZoneInfo = __import__("zoneinfo").ZoneInfo
            assert compute_age(date(2000, 2, 28)) == 25

    def test_leap_year_birthday_not_yet(self) -> None:
        """非闰年 2/28：出生 2000-03-01，今天还未到生日，age = 24。"""
        # 出生 2000-03-01；上海时间 2025-02-28
        # today=(2,28) < birth=(3,1) → birthday not yet → age = 25-1 = 24
        with patch("app.chat.prompts.datetime") as mock_dt:
            mock_dt.now.return_value = __import__("datetime").datetime(2025, 2, 28)
            mock_dt.ZoneInfo = __import__("zoneinfo").ZoneInfo
            assert compute_age(date(2000, 3, 1)) == 24

    def test_utc_midnight_shanghai_still_yesterday(self) -> None:
        """UTC 0:30 但 Shanghai 是 8:30（昨天23:30）—— age 不增一岁。

        验证 compute_age 用 ZoneInfo("Asia/Shanghai") 而非 UTC date.today()。
        若用 UTC，今天=2025-03-01，birth=2000-03-01 → 25岁。
        用 Shanghai，今天=2025-02-28，birth=2000-03-01 → 24岁。
        """
        # 场景：上海时间 2025-02-28 23:30（UTC 2025-03-01 15:30）
        # mock 上海时间 = 2025-02-28
        with patch("app.chat.prompts.datetime") as mock_dt:
            mock_dt.now.return_value = __import__("datetime").datetime(2025, 2, 28, 23, 30)
            mock_dt.ZoneInfo = __import__("zoneinfo").ZoneInfo
            # 2000-03-01 出生，上海时间 2025-02-28 尚未过生日 → 24岁
            assert compute_age(date(2000, 3, 1)) == 24


class TestBuildSystemPrompt:
    """关注点 2-5：build_system_prompt 结构和契约"""

    # ---- 关注点 2：gender 4 状态 ----

    def test_gender_male_has_gender_section(self) -> None:
        content = build_system_prompt(12, "male").content
        assert "# 关于对方的性别" in content
        assert STUB_GENDER_MALE in content

    def test_gender_female_has_gender_section(self) -> None:
        content = build_system_prompt(12, "female").content
        assert "# 关于对方的性别" in content
        assert STUB_GENDER_FEMALE in content

    def test_gender_unknown_omits_section(self) -> None:
        """unknown → 整段省略，不留空标题。"""
        content = build_system_prompt(12, "unknown").content
        assert "# 关于对方的性别" not in content

    def test_gender_none_omits_section(self) -> None:
        """None → 整段省略，不留空标题。"""
        content = build_system_prompt(12, None).content
        assert "# 关于对方的性别" not in content

    # ---- 关注点 3：签名拒绝多余字段 ----

    def test_rejects_extra_field_concerns(self) -> None:
        with pytest.raises(TypeError):
            build_system_prompt(12, "male", concerns="test")  # pyright: ignore[reportCallIssue]

    def test_rejects_extra_field_sensitivity(self) -> None:
        with pytest.raises(TypeError):
            build_system_prompt(12, "male", sensitivity=0.5)  # pyright: ignore[reportCallIssue]

    def test_rejects_extra_field_custom_redlines(self) -> None:
        with pytest.raises(TypeError):
            build_system_prompt(12, "male", custom_redlines=["x"])  # pyright: ignore[reportCallIssue]

    def test_rejects_extra_field_birth_date(self) -> None:
        with pytest.raises(TypeError):
            build_system_prompt(12, "male", birth_date=date(2013, 3, 1))  # pyright: ignore[reportCallIssue]

    def test_content_rejects_dict_fields(self) -> None:
        """运行时：content 字符串不包含字段名字面值。"""
        content = build_system_prompt(12, "male").content
        assert "concerns" not in content
        assert "sensitivity" not in content
        assert "custom_redlines" not in content
        assert "birth_date" not in content

    # ---- 关注点 4：tier 10 边界值落档 ----

    @pytest.mark.parametrize(
        "age,expected_stub",
        [
            (3, STUB_TIER_EARLY_CHILDHOOD),
            (4, STUB_TIER_EARLY_CHILDHOOD),
            (5, STUB_TIER_EARLY_CHILDHOOD),
            (6, STUB_TIER_LATE_CHILDHOOD),
            (7, STUB_TIER_LATE_CHILDHOOD),
            (8, STUB_TIER_LATE_CHILDHOOD),
            (9, STUB_TIER_LATE_CHILDHOOD),
            (10, STUB_TIER_PRE_TEEN),
            (13, STUB_TIER_PRE_TEEN),
            (14, STUB_TIER_TEEN),
            (18, STUB_TIER_TEEN),
            (19, STUB_TIER_YOUNG_ADULT),
            (21, STUB_TIER_YOUNG_ADULT),
        ],
    )
    def test_tier_boundaries(self, age: int, expected_stub: str) -> None:
        content = build_system_prompt(age, None).content
        assert expected_stub in content

    # ---- 关注点 5：age 字面值仅在末段 ----

    def test_age_literal_only_in_last_section(self) -> None:
        """age 字面值不能出现在前 4 个章节（L1→L4 对 prefix cache 不含动态内容）。"""
        content = str(build_system_prompt(12, "male").content)
        sections = content.split("# 当前对话上下文")
        assert len(sections) == 2
        prefix = sections[0]
        last_section = sections[1]
        assert "12" not in prefix, "age literal must not appear before last section"
        assert "12" in last_section
        assert "对方今年 12 岁。" in last_section

    @pytest.mark.parametrize("age", [3, 5, 9, 13, 18, 19, 21])
    def test_age_literal_only_in_last_section_all_tiers(self, age: int) -> None:
        content = str(build_system_prompt(age, None).content)
        sections = content.split("# 当前对话上下文")
        prefix = sections[0]
        last_section = sections[1]
        assert str(age) not in prefix
        assert f"对方今年 {age} 岁。" in last_section

    # ---- 5 章节结构顺序（5-sections 版本）----

    def test_section_order_5_chapters(self) -> None:
        """male/female → 5 章节严格按序：身份→安全→对话风格→性别→上下文。"""
        content = str(build_system_prompt(12, "male").content)
        order = [
            "# 身份与原则",
            "# 安全底线",
            "# 对话风格",
            "# 关于对方的性别",
            "# 当前对话上下文",
        ]
        positions = [content.find(s) for s in order]
        assert positions == sorted(positions), f"sections out of order: {positions}"

    # ---- 4 章节结构顺序（gender omitted 版本）----

    def test_section_order_4_chapters_gender_unknown(self) -> None:
        """unknown/null → 4 章节严格按序：身份→安全→对话风格→上下文。"""
        content = build_system_prompt(12, "unknown").content
        order = [
            "# 身份与原则",
            "# 安全底线",
            "# 对话风格",
            "# 关于对方的性别",  # 必须不存在
            "# 当前对话上下文",
        ]
        # 前3个存在且有序
        content_str = str(content)
        positions = [content_str.find(s) for s in order[:3]]
        assert positions == sorted(positions)
        # "# 关于对方的性别" 不存在（find==-1）
        assert content_str.find("# 关于对方的性别") == -1
        # 上下文在最后
        assert content_str.find("# 当前对话上下文") > content_str.find("# 对话风格")
        # 无残留空标题（如 "# 关于对方的性别\n\n# 对话风格"）
        assert "# 关于对方的性别\n\n#" not in content_str


class TestStubCount:
    """关注点 6：TODO(prompts-content) grep 命中 12 处"""

    def test_todo_content_slots_count(self) -> None:
        """实际 slot 数：9 处 stub 函数注释 + 2 处说明性文字 + 1 COMPRESSION_PROMPT_STUB = 12。

        关注点 6 要求 "grep 命中数与实际 stub 函数数一致"——
        9 个 stub 函数（_identity_block/_safety_block 各1 + _tier_block 5档 + _gender_block 2状态）
        均在函数体内有 # TODO(prompts-content) 行。
        M6-patch3 Step 2 新增 COMPRESSION_PROMPT_STUB 含第 12 处。
        """
        import subprocess

        result = subprocess.run(
            ["grep", "-c", "TODO(prompts-content)", "app/chat/prompts.py"],
            capture_output=True,
            text=True,
            cwd="/app",
        )
        count = int(result.stdout.strip())
        # 9 stub function body comments + 2 explanatory + 1 COMPRESSION_PROMPT_STUB = 12
        assert count == 12, f"expected 12 TODO(prompts-content) lines, got {count}"

