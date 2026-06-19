"""时区与逻辑日中性的工具。

`core/*` 不带任何 session 域业务规则：`logical_day` 默认 `boundary_hour=0`
（自然日历日），session 域的 `4` 点切日规则由 `app/domain/chat/session_policy.py`
薄包装传入，本文件不固化业务偏移。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
"""项目统一使用的时区(Asia/Shanghai),所有逻辑日计算都基于此。"""


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
