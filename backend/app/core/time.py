"""时区工具:零业务依赖 core/* 段。

D-4A.2:D-1 边界 — core/* 不带 session 域 magic constant。
SESSION_HARD_BOUNDARY_HOUR=4 是 session 域业务规则,放 domain/chat/session_policy.py。
本文件只提供中性时区工具:SHANGHAI + logical_day(ts, boundary_hour=0)
默认 boundary_hour=0(自然日历日,无业务偏移)。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def logical_day(ts: datetime, boundary_hour: int = 0) -> date:
    """将带时区时间戳映射到逻辑日:(ts - boundary_hour).date()。

    默认 boundary_hour=0(自然日历日)。Session 域薄包装传入 4h
    硬边界以维持原 4am 日切语义。

    当 boundary_hour=4 时,当日 04:00–次日 03:59 为同一逻辑日。
    """
    if ts.tzinfo is None:
        raise ValueError("ts must be timezone-aware")
    return (ts.astimezone(SHANGHAI) - timedelta(hours=boundary_hour)).date()
