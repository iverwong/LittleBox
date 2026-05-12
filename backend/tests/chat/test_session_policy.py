"""session_policy 纯函数单元测试。

覆盖 should_switch_session 全部分支 + today_session_title 中文格式
+ logical_day 时区感知校验。
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.chat.session_policy import (
    SHANGHAI,
    SESSION_HARD_BOUNDARY_HOUR,
    SESSION_IDLE_THRESHOLD_MINUTES,
    SESSION_IDLE_WINDOW,
    logical_day,
    should_switch_session,
    today_session_title,
)

_UTC8 = ZoneInfo("Asia/Shanghai")


# ---- should_switch_session 参数化 ----
# last_active_at 与 now 的差值驱动日切判定
# (last_h, last_m, now_h, now_m, expected)
# 所有时间在同一天 2026-05-11，时区 Asia/Shanghai

@pytest.mark.parametrize("last_h,last_m,now_h,now_m,expected", [
    # 凌晨空闲阈值边界
    (23, 0,  0, 30, False),   # 同 logical_day（4h 硬边界前），未达空闲 30 分
    (23, 30, 0, 55, False),   # 同 logical_day，未达空闲 30 分（差 5 分）
    ( 0, 50, 3, 50, True),    # 凌晨窗口内，差 3h > 30 分 → 切
    ( 2, 0,  2, 25, False),   # 凌晨窗口内，差 25 分 < 30 分 → 不切
    ( 1, 0,  1, 45, True),    # 凌晨窗口内，差 45 分 > 30 分 → 切
    # 跨 4am 硬边界
    ( 3, 50, 4,  1, True),    # 跨硬切点（03:59→04:00 属不同 logical_day）
    ( 3, 50, 4, 10, True),    # 跨硬切点 + 差 20 分
    # 同 logical_day 白天
    ( 2, 0, 10,  0, True),    # 跨 4am（凌晨 2→10 硬边界触发）
    ( 8, 0, 14,  0, False),   # 同 logical_day 白天，差 6h → 不切
])
def test_should_switch_session(last_h, last_m, now_h, now_m, expected):
    """参数化覆盖 9 种日切场景（规则 1 + 规则 2 全部边界）。"""
    base = datetime(2026, 5, 11, tzinfo=_UTC8)
    last = base.replace(hour=last_h, minute=last_m)
    now = base.replace(hour=now_h, minute=now_m)
    # 当 now < last（时间倒退）时加一天
    if now <= last:
        now += timedelta(days=1)
    assert should_switch_session(last, now) is expected


def test_should_switch_session_null():
    """last_active_at=None → 应切。"""
    assert should_switch_session(None, datetime.now(SHANGHAI)) is True


def test_today_session_title_cn_format():
    """中文标题格式：`周一 · 5月11日`。"""
    assert today_session_title(
        datetime(2026, 5, 11, 14, 30, tzinfo=SHANGHAI)
    ) == "周一 · 5月11日"


def test_today_session_title_default_no_arg():
    """不传 now 参数不报错，返回今日格式字符串。"""
    title = today_session_title()
    assert "周" in title and "月" in title and "日" in title


def test_logical_day_4am_boundary():
    """03:59 与 04:00 属不同 logical_day。"""
    d1 = logical_day(datetime(2026, 5, 11, 3, 59, tzinfo=SHANGHAI))
    d2 = logical_day(datetime(2026, 5, 11, 4, 0, tzinfo=SHANGHAI))
    assert d1 != d2
    assert d1 == d2 - timedelta(days=1)


def test_logical_day_rejects_naive():
    """无时区 datetime → raise ValueError。"""
    from datetime import datetime as dt_naive
    with pytest.raises(ValueError, match="timezone-aware"):
        logical_day(dt_naive(2026, 5, 11, 14, 30))


def test_session_policy_constants():
    """常量值与计划一致。"""
    assert SESSION_HARD_BOUNDARY_HOUR == 4
    assert SESSION_IDLE_WINDOW == (1, 4)
    assert SESSION_IDLE_THRESHOLD_MINUTES == 30
