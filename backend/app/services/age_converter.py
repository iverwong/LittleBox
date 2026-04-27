"""age ↔ birth_date 换算服务（M4.8 B2）。纯函数，无 IO 依赖。"""
from __future__ import annotations

from datetime import date


def age_to_birth_date(age: int, ref: date | None = None) -> date:
    """将 age（岁）转换为近似 birth_date。

    算法：ref - age 年同月同日；闰年 2-29 在非闰年触发 ValueError，兜底为前一日。

    Args:
        age: 年龄，必须在 [3, 21] 范围内。
        ref: 基准日期，默认为 date.today()。

    Returns:
        近似出生日期。

    Raises:
        ValueError: age 不在 [3, 21] 范围内。
    """
    if not (3 <= age <= 21):
        raise ValueError(f"age must be in [3, 21], got {age}")

    ref = ref if ref is not None else date.today()
    try:
        return date(ref.year - age, ref.month, ref.day)
    except ValueError:
        # 2-29 in non-leap year → fall back to 2-28
        return date(ref.year - age, ref.month, ref.day - 1)


def birth_date_to_age(birth_date: date, ref: date | None = None) -> int:
    """将 birth_date 转换为 age（岁）。

    钳位到 [3, 21] 范围，避免极端 birth_date 把 API 打挂。

    Args:
        birth_date: 出生日期。
        ref: 基准日期，默认为 date.today()。

    Returns:
        3–21 之间的年龄。
    """
    ref = ref if ref is not None else date.today()
    raw = ref.year - birth_date.year - (
        (ref.month, ref.day) < (birth_date.month, birth_date.day)
    )
    return max(3, min(21, raw))
