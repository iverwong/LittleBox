"""Session 日切策略:4h 硬边界 + 凌晨空闲 30 分切换 + 中文日期标题。

SHANGHAI 时区与通用 logical_day 工具位于 `app/core/time.py`(零业务依赖);
本文件保留业务规则,薄包装 `logical_day(ts)` 转发到 `core.time.logical_day`,
把 `SESSION_HARD_BOUNDARY_HOUR` 注入到该工具函数。调用方不感知拆分,churn 最小。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app.core.time import SHANGHAI
from app.core.time import logical_day as _core_logical_day

SESSION_HARD_BOUNDARY_HOUR = 4
SESSION_IDLE_WINDOW = (1, 4)
SESSION_IDLE_THRESHOLD_MINUTES = 30
DAILY_SUMMARY_TRIGGER_HOUR = 5  # 日终专家域使用
DEFAULT_DAILY_NOTIFY_TIME = time(8, 0)  # UI 落点

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def logical_day(ts: datetime) -> date:
    """Session 域薄包装:用 SESSION_HARD_BOUNDARY_HOUR=4 调 core.time.logical_day。

    对外保持 `logical_day(ts)` 单参签名,所有调用点(me.py / pipeline.py / tests)
    改 import 路径即可,行为 byte 级不变。

    Args:
        ts: 带时区的时间戳。

    Returns:
        该时间戳归属的逻辑日期。
    """
    return _core_logical_day(ts, SESSION_HARD_BOUNDARY_HOUR)


def should_switch_session(last_active_at: datetime | None, now: datetime) -> bool:
    """判断是否需要切换到新 session。

    规则:
      1. 硬切:logical_day(last) != logical_day(now) → 切
      2. 凌晨空闲:now.hour ∈ [1, 4) 且 now - last > 30min → 切

    Args:
        last_active_at: 上一活跃时间,None 视为需要新建。
        now: 当前时间。

    Returns:
        True 表示应新建 session,False 表示沿用旧 session。
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
    """生成今日 session 的中文标题,格式 `周一 · 5月11日`。

    Args:
        now: 当前时间,缺省取本地 SHANGHAI 当前时间。

    Returns:
        中文格式标题字符串。
    """
    now = now or datetime.now(SHANGHAI)
    d = logical_day(now)
    return f"{_WEEKDAY_CN[d.weekday()]} · {d.month}月{d.day}日"
