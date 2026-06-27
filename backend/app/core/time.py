"""时区与逻辑日中性的工具。

`core/*` 不带任何 session 域业务规则：`logical_day` 默认 `boundary_hour=0`
（自然日历日），session 域的 `4` 点切日规则由 `app/domain/chat/session_policy.py`
薄包装传入，本文件不固化业务偏移。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
"""项目统一使用的时区(Asia/Shanghai),所有逻辑日计算都基于此。"""

UTC_TZ = UTC
"""重新导出 stdlib UTC，供全项目统一 import 来源，避免各文件分别 from datetime import UTC。"""


def now_utc() -> datetime:
    """返回当前 UTC 时间（tz-aware）。

    全项目「此刻」取值的唯一来源。替代分散在各处的
    ``datetime.now(UTC)`` / ``datetime.now(timezone.utc)``。
    """
    return datetime.now(UTC)


def now_shanghai() -> datetime:
    """返回当前 Asia/Shanghai 时间（tz-aware）。"""
    return datetime.now(SHANGHAI)


def same_natural_day(a: datetime, b: datetime) -> bool:
    """两个带时区的时间戳是否落在同一自然日(Asia/Shanghai)。

    与 `logical_day(ts, boundary_hour=0)` 等价,但更便于 R1 判定
    ("now 与 last_create_at 是否同日")。两侧任一为 naive → raise。

    Args:
        a: 带时区的时间戳。
        b: 带时区的时间戳。

    Returns:
        True 表示两个时间戳的 Shanghai 自然日相同。

    Raises:
        ValueError: 任一参数不带时区信息。
    """
    if a.tzinfo is None or b.tzinfo is None:
        raise ValueError("a and b must be timezone-aware")
    return a.astimezone(SHANGHAI).date() == b.astimezone(SHANGHAI).date()


def logical_day(ts: datetime, boundary_hour: int = 0) -> date:
    """将带时区时间戳映射到逻辑日。

    算法：先把时间戳切到 SHANGHAI 时区，再减去 `boundary_hour` 小时，最后取日期部分。
    当 `boundary_hour=4` 时，04:00 至次日 03:59 视为同一逻辑日。

    默认 `boundary_hour=0` 对应自然日历日；session 域通过包装函数传入 `4`
    以维持凌晨切日语义。

    Args:
        ts: 带时区的时间戳。
        boundary_hour: 切日偏移小时数。默认 0。

    Returns:
        逻辑日对应的 `date`。

    Raises:
        ValueError: `ts` 不带时区信息。
    """
    if ts.tzinfo is None:
        raise ValueError("ts must be timezone-aware")
    return (ts.astimezone(SHANGHAI) - timedelta(hours=boundary_hour)).date()


def age_at(birth_date: date, tz: str = "Asia/Shanghai") -> int:
    """按生日计算到今日的整年龄。

    算法：在指定时区取「今天」，先按年份差估算，再在尚未过生日时减一。
    与 `chat/prompts.py::compute_age` 字节等价；该函数从 prompt 模块提到本文件
    是因为「按生日算年龄」属于中性时区工具，不属于 prompt 关注点。

    Args:
        birth_date: 出生日期。
        tz: 计算「今天」所用时区，字符串形式的 IANA 时区名，默认 `Asia/Shanghai`。

    Returns:
        整年龄（岁）。
    """
    today = datetime.now(ZoneInfo(tz)).date()
    years = today.year - birth_date.year
    if (today.month, today.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years
