"""所有 LLM prompt 字符串单一来源 = 本文件。

外部仅通过 import 函数 / 常量访问，禁止在其他模块内联 prompt 字面量。

当前内容：
- build_system_prompt — 主对话 5 段 system prompt（年龄 + 性别驱动）
- COMPRESSION_PROMPT_STUB — M8 上下文压缩 prompt 占位
- build_compression_prompt — 同上，返回 SystemMessage 包装

9 个 TODO(prompts-content) slot 待专人审核后填充。
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import SystemMessage

# ---- Stub constants (stable, assertable in tests) ----
STUB_IDENTITY = "[STUB identity]"
STUB_SAFETY = "[STUB safety]"
STUB_TIER_EARLY_CHILDHOOD = "[STUB tier:early_childhood]"
STUB_TIER_LATE_CHILDHOOD = "[STUB tier:late_childhood]"
STUB_TIER_PRE_TEEN = "[STUB tier:pre_teen]"
STUB_TIER_TEEN = "[STUB tier:teen]"
STUB_TIER_YOUNG_ADULT = "[STUB tier:young_adult]"
STUB_GENDER_MALE = "[STUB gender:male]"
STUB_GENDER_FEMALE = "[STUB gender:female]"
# Total: 9 TODO(prompts-content) slots


def compute_age(birth_date: date, tz: str = "Asia/Shanghai") -> int:
    """Compute age as of today in the given timezone.

    Uses zone-aware date so that UTC midnight vs Asia/Shanghai midnight
    boundary cases are handled correctly.
    """
    today = datetime.now(ZoneInfo(tz)).date()
    years = today.year - birth_date.year
    if (today.month, today.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years


def _identity_block() -> str:
    # TODO(prompts-content): identity & dialogue principles template
    return STUB_IDENTITY


def _safety_block() -> str:
    # TODO(prompts-content): jailbreak resistance template
    return STUB_SAFETY


def _tier_block(age: int) -> str:
    if age <= 5:
        # TODO(prompts-content): early_childhood (3-5)
        return STUB_TIER_EARLY_CHILDHOOD
    if age <= 9:
        # TODO(prompts-content): late_childhood (6-9)
        return STUB_TIER_LATE_CHILDHOOD
    if age <= 13:
        # TODO(prompts-content): pre_teen (10-13)
        return STUB_TIER_PRE_TEEN
    if age <= 18:
        # TODO(prompts-content): teen (14-18)
        return STUB_TIER_TEEN
    # TODO(prompts-content): young_adult (19-21, incl. "20+")
    return STUB_TIER_YOUNG_ADULT


def _gender_block(gender: str | None) -> str | None:
    if gender == "male":
        # TODO(prompts-content): male gender block
        return STUB_GENDER_MALE
    if gender == "female":
        # TODO(prompts-content): female gender block
        return STUB_GENDER_FEMALE
    # unknown / None → omit entire section
    return None


def build_system_prompt(age: int, gender: str | None) -> SystemMessage:
    """Build the 5-section system prompt.

    Section order (cache-optimized, baseline §7.3 L1→L5):
      1. 身份与原则
      2. 安全底线
      3. 对话风格   (age-dependent tier)
      4. 关于对方的性别 (gender block; None/unknown → section omitted)
      5. 当前对话上下文 (age literal only here — prefix-cache constraint)

    Signature accepts ONLY (age: int, gender: str | None).
    Rejects any extra field at call site (TypeError).
    """
    parts: list[str] = []
    parts.append(f"# 身份与原则\n{_identity_block()}")
    parts.append(f"# 安全底线\n{_safety_block()}")
    parts.append(f"# 对话风格\n{_tier_block(age)}")
    g = _gender_block(gender)
    if g is not None:
        parts.append(f"# 关于对方的性别\n{g}")
    parts.append(f"# 当前对话上下文\n对方今年 {age} 岁。")
    return SystemMessage(content="\n\n".join(parts))


# ---- 摘要前缀（context.py build_context 使用） ----

SUMMARY_PREFIX = "[历史对话摘要]\n"

# ---- M8 上下文压缩 prompt（占位，scheme R 已移至 compression.py） ----

COMPRESSION_PROMPT_STUB = "TODO(prompts-content): compression instruction"

