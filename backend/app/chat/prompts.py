"""所有 LLM prompt 字符串单一来源 = 本文件。

外部仅通过 import 函数 / 常量访问，禁止在其他模块内联 prompt 字面量。

当前内容：
- build_system_prompt — 主对话 5 段 system prompt（年龄 + 性别驱动）
- COMPRESSION_PROMPT_STUB — M8 上下文压缩 prompt 占位
- build_compression_prompt — 同上，返回 SystemMessage 包装
- build_crisis_system_prompt — 危机接管 system prompt（tier/gender 复用主对话分段）
- build_redline_system_prompt — 红线接管 system prompt（同上）
- format_reentry_wrapper_crisis — crisis 重入 wrapper（{user_input} 占位）
- format_reentry_wrapper_redline — redline 重入 wrapper（{user_input} 占位）
- format_guidance_wrapper — 引导注入 wrapper（{user_input} + {guidance} 占位）

14 个 prompt 占位 slot 待专人审核后填充。
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
# Total: 14 prompt slots


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

# ---- M8 上下文压缩 prompt 任务说明（与审查关注点解耦，不含情绪 / 风险 / 安全语境） ----

COMPRESSION_PROMPT_STUB = (
    "用第三人称把下面这段对话压缩为一段简短叙述，"
    "保留聊过的话题、对方分享过的事和喜好、提到的人或物、约定要一起做的事。"
    "不复述完整对白。"
)


# ---- M9 crisis anchor_window 前缀（§D.1，供 context.py 引用） ----

ANCHOR_WINDOW_PREFIX = "[anchor 窗口]"

# ---- M9 三级干预 STUB prompt + wrapper（14 个 TODO slot 中新增的 5 个） ----

# C.1
STUB_CRISIS_SYSTEM_PROMPT = (
    "# TODO(prompts-content): crisis 接管身份与安全底线\n"
    "[STUB crisis intervention system prompt]"
)

# C.2
STUB_REDLINE_SYSTEM_PROMPT = (
    "# TODO(prompts-content): redline 接管身份与安全底线\n"
    "[STUB redline intervention system prompt]"
)

# C.3
STUB_REENTRY_WRAPPER_CRISIS = (
    "TODO(prompts-content): crisis 重入 wrapper\n"
    "用户输入：{user_input}"
)

# C.4
STUB_REENTRY_WRAPPER_REDLINE = (
    "TODO(prompts-content): redline 重入 wrapper\n"
    "用户输入：{user_input}"
)

# C.5（guidance 为空时透传 user_input，不包装）
STUB_GUIDANCE_WRAPPER = (
    "TODO(prompts-content): 引导注入 wrapper\n"
    "用户输入：{user_input}\n"
    "引导建议：{guidance}"
)


def build_crisis_system_prompt(age: int, gender: str | None) -> SystemMessage:
    """危机接管 system prompt，5 段结构同 build_system_prompt。"""
    parts: list[str] = []
    parts.append(f"# 身份与原则\n{STUB_CRISIS_SYSTEM_PROMPT}")
    parts.append(f"# 安全底线\n{STUB_CRISIS_SYSTEM_PROMPT}")
    parts.append(f"# 对话风格\n{_tier_block(age)}")
    g = _gender_block(gender)
    if g is not None:
        parts.append(f"# 关于对方的性别\n{g}")
    parts.append(f"# 当前对话上下文\n对方今年 {age} 岁。")
    return SystemMessage(content="\n\n".join(parts))


def build_redline_system_prompt(age: int, gender: str | None) -> SystemMessage:
    """红线接管 system prompt，5 段结构同 build_system_prompt。"""
    parts: list[str] = []
    parts.append(f"# 身份与原则\n{STUB_REDLINE_SYSTEM_PROMPT}")
    parts.append(f"# 安全底线\n{STUB_REDLINE_SYSTEM_PROMPT}")
    parts.append(f"# 对话风格\n{_tier_block(age)}")
    g = _gender_block(gender)
    if g is not None:
        parts.append(f"# 关于对方的性别\n{g}")
    parts.append(f"# 当前对话上下文\n对方今年 {age} 岁。")
    return SystemMessage(content="\n\n".join(parts))


def format_reentry_wrapper_crisis(user_input: str) -> str:
    """crisis 重入 wrapper：包装用户输入后送入 crisis LLM。"""
    return STUB_REENTRY_WRAPPER_CRISIS.format(user_input=user_input)


def format_reentry_wrapper_redline(user_input: str) -> str:
    """redline 重入 wrapper：包装用户输入后送入 redline LLM。"""
    return STUB_REENTRY_WRAPPER_REDLINE.format(user_input=user_input)


def format_guidance_wrapper(user_input: str, guidance: str | None) -> str:
    """引导注入 wrapper：guidance 为空时透传 user_input。"""
    if not guidance:
        return user_input
    return STUB_GUIDANCE_WRAPPER.format(user_input=user_input, guidance=guidance)

