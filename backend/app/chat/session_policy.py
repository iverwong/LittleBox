"""Session 日切策略：4h 硬边界 + 凌晨空闲 30 分切换 + 中文日期标题。

纯函数模块，零副作用，便于单测。
M8 / M9 升级时本文件保持 stable 不动。
"""
from __future__ import annotations
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")

SESSION_HARD_BOUNDARY_HOUR = 4
SESSION_IDLE_WINDOW = (1, 4)
SESSION_IDLE_THRESHOLD_MINUTES = 30
DAILY_SUMMARY_TRIGGER_HOUR = 5  # M8 / M9 用
DEFAULT_DAILY_NOTIFY_TIME = time(8, 0)  # M9 落 UI

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def logical_day(ts: datetime) -> date:
    """将带时区时间戳映射到逻辑日：(ts - 4h).date()。

    当日 04:00–次日 03:59 为同一逻辑日。
    """
    if ts.tzinfo is None:
        raise ValueError("ts must be timezone-aware")
    return (ts.astimezone(SHANGHAI) - timedelta(hours=SESSION_HARD_BOUNDARY_HOUR)).date()


def should_switch_session(last_active_at: datetime | None, now: datetime) -> bool:
    """判断是否需要切换 session。

    规则 1（硬切）：logical_day(last) != logical_day(now) → 切
    规则 2（凌晨空闲）：now.hour ∈ [1,4) 且 now - last > 30min → 切
    """
    if last_active_at is None:
        return True
    if logical_day(last_active_at) != logical_day(now):
        return True
    now_local = now.astimezone(SHANGHAI)
    if SESSION_IDLE_WINDOW[0] <= now_local.hour < SESSION_IDLE_WINDOW[1]:
        if now - last_active_at > timedelta(minutes=SESSION_IDLE_THRESHOLD_MINUTES):
            return True
    return False


def today_session_title(now: datetime | None = None) -> str:
    """返回 `周一 · 5月11日` 中文格式标题。"""
    now = now or datetime.now(SHANGHAI)
    d = logical_day(now)
    return f"{_WEEKDAY_CN[d.weekday()]} · {d.month}月{d.day}日"
