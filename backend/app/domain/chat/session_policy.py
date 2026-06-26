"""Session 日切策略:自然日为单位 + 跨日 30 分钟宽限 + 中文日期标题。

SHANGHAI 时区与通用 `logical_day` / `same_natural_day` 工具位于
`app/core/time.py`(零业务依赖叶子);本文件保留业务规则,
薄包装 `logical_day(ts)` 转发到 `core.time.logical_day(ts, 0)`,
调用方不感知拆分,churn 最小。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app.core.time import SHANGHAI, now_shanghai
from app.core.time import logical_day as _core_logical_day
from app.core.time import same_natural_day

# Session 域的可调常量。
# 旧策略(硬切 04:00 + 软切窗口 [1, 4))已下线,改为自然日 + 跨日宽限 + 04:00 硬切:
#   R1  同自然日(create_at vs now)         → 不切
#   R2  跨自然日 + gap(last_active, now) > 30min  → 切
#   R3  跨自然日 + gap ≤ 30min + now ≥ 04:00  → 切(实际不可达:跨日时 gap 至少 24h)
#   R3' 跨自然日 + gap ≤ 30min + now < 04:00  → 不切
SESSION_DAY_BOUNDARY_HOUR = 0
SESSION_CROSS_DAY_GRACE_MINUTES = 30
SESSION_CROSS_DAY_HARD_CUT_HOUR = 4

DAILY_SUMMARY_TRIGGER_HOUR = 4  # 日终专家域使用,仅作参考常量保留
DEFAULT_DAILY_NOTIFY_TIME = time(8, 0)  # UI 落点

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def logical_day(ts: datetime) -> date:
    """Session 域薄包装:用 SESSION_DAY_BOUNDARY_HOUR=0 调 core.time.logical_day。

    业务语义:返回上海时区下该时间戳的自然日历日。
    调用方通过本函数而非直接 import core.time.logical_day,以便未来
    若业务规则再变(例如换切日小时),只需改本文件。

    Args:
        ts: 带时区的时间戳。

    Returns:
        该时间戳归属的自然日历日(`date`)。
    """
    return _core_logical_day(ts, SESSION_DAY_BOUNDARY_HOUR)


def should_switch_session(
    last_active_at: datetime | None,
    last_create_at: datetime | None,
    now: datetime,
) -> bool:
    """判断是否需要切换到新 session。

    规则(详见模块 docstring):
      R1  same_natural_day(last_create_at, now)  → 不切
      R2  now - last_active_at > 30min           → 切
      R3  cross-day + now.hour ≥ 04:00           → 切
      R3' cross-day + now.hour < 04:00           → 不切

    Args:
        last_active_at: 上一活跃时间,None 视为需要新建。
        last_create_at: 上一 session 的创建时间,None 视为需要新建。
            用于 R1 判定(锚定自然日),即便 session 经 R3' 跨过 0 点
            仍以 create_at 的自然日为准。
        now: 当前时间。

    Returns:
        True 表示应新建 session,False 表示沿用旧 session。
    """
    if last_active_at is None or last_create_at is None:
        return True
    # R1: 同一自然日(create_at vs now)→ 不切
    if same_natural_day(last_create_at, now):
        return False
    # R2: 跨日 + gap(last_active, now) > 30min → 切
    if now - last_active_at > timedelta(minutes=SESSION_CROSS_DAY_GRACE_MINUTES):
        return True
    # R3 / R3': 跨日 + gap ≤ 30min,看 now 是否过 04:00 硬切点
    now_local = now.astimezone(SHANGHAI)
    return now_local.hour >= SESSION_CROSS_DAY_HARD_CUT_HOUR


def today_session_title(now: datetime | None = None) -> str:
    """生成今日 session 的中文标题,格式 `周一 · 5月11日`。

    基于自然日历日(与 should_switch_session 的 R1 对齐)。
    旧实现(boundary=4)在 02:00 会返回"昨日";新实现返回当天。
    这是 product-visible 的语义变化,UX 需知悉。

    Args:
        now: 带时区的当前时间,缺省取本地 SHANGHAI 当前时间。

    Returns:
        中文格式标题字符串。
    """
    now = now or now_shanghai()
    d = logical_day(now)
    return f"{_WEEKDAY_CN[d.weekday()]} · {d.month}月{d.day}日"
