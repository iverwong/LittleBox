"""session_policy 纯函数单元测试。

覆盖 should_switch_session 全部分支(R1 / R2 / R3') + today_session_title 中文格式
+ 自然日语义在 02:00 的标题归属。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core.time import SHANGHAI
from app.domain.chat.session_policy import (
    SESSION_CROSS_DAY_GRACE_MINUTES,
    SESSION_CROSS_DAY_HARD_CUT_HOUR,
    SESSION_DAY_BOUNDARY_HOUR,
    logical_day,
    should_switch_session,
    today_session_title,
)


# ---- should_switch_session 参数化 ----
# 三规则:
#   R1  同自然日(按 create_at 与 now 的日期)             → 不切
#   R2  跨自然日 + gap(last_active, now) > 30min         → 切
#   R3' 跨自然日 + gap ≤ 30min + now < 04:00            → 不切
#
# 备注:R3(cross-day + gap ≤ 30min + now ≥ 04:00)与 R2 在跨日场景下
# 互斥(跨日时 gap 至少 24h > 30min,R2 必然先命中),故 R3 实际不可测;
# 此处仅覆盖可测的 R1 / R2 / R3'。
#
# 参数格式: (last_date_str, last_h, last_m, now_date_str, now_h, now_m, expected)

@pytest.mark.parametrize(
    "last_date,last_h,last_m,now_date,now_h,now_m,expected",
    [
        # R1: 同日任意 gap 都不切
        ("2026-05-11", 8, 0, "2026-05-11", 14, 0, False),    # 同日 gap 6h
        ("2026-05-11", 1, 0, "2026-05-11", 2, 25, False),    # 同日凌晨 gap 85min
        ("2026-05-11", 13, 0, "2026-05-11", 23, 59, False),  # 同日任意
        # R2: 跨日 + 大 gap → 切
        ("2026-05-10", 23, 0, "2026-05-11", 2, 0, True),     # 跨日 gap 3h
        ("2026-05-10", 23, 30, "2026-05-11", 0, 5, True),    # 跨日 gap 35min
        ("2026-05-10", 23, 30, "2026-05-11", 1, 0, True),    # 跨日 gap 1.5h
        # 跨日大 gap + 已过 04:00 → 切(走 R2)
        ("2026-05-10", 23, 55, "2026-05-11", 4, 5, True),    # 跨日 gap 4h 10min, 04:05
        ("2026-05-10", 3, 50, "2026-05-11", 4, 0, True),     # 跨日 gap 24h 10min, 04:00 边界
        # R3': 跨日 + gap ≤ 30min + now < 04:00 → 不切
        ("2026-05-10", 23, 55, "2026-05-11", 0, 5, False),   # 跨日 30min 宽限
        ("2026-05-10", 23, 30, "2026-05-11", 0, 0, False),   # 跨日 30min 宽限(整点)
        ("2026-05-10", 23, 35, "2026-05-11", 0, 0, False),   # 跨日 gap 25min, 宽限 (R3')
    ],
)
def test_should_switch_session(last_date, last_h, last_m, now_date, now_h, now_m, expected):
    """参数化覆盖 R1 / R2 / R3' 全部边界。"""
    last_dt = datetime.fromisoformat(f"{last_date}T{last_h:02d}:{last_m:02d}:00").replace(tzinfo=SHANGHAI)
    now_dt = datetime.fromisoformat(f"{now_date}T{now_h:02d}:{now_m:02d}:00").replace(tzinfo=SHANGHAI)
    assert should_switch_session(last_dt, last_dt, now_dt) is expected


def test_should_switch_session_null_last_active():
    """last_active_at=None → 应切(视为需新建)。"""
    now = datetime.now(SHANGHAI)
    create = now - timedelta(hours=1)
    assert should_switch_session(None, create, now) is True


def test_should_switch_session_null_last_create():
    """last_create_at=None → 应切(视为需新建)。"""
    now = datetime.now(SHANGHAI)
    assert should_switch_session(now, None, now) is True


def test_should_switch_session_create_active_now_different_days():
    """create 与 active 不同日 + now 与 create 不同日 → R1 不命中,走 R2。

    create=T-1 23:30(昨日),active=T0 02:00(今晨),now=T0 09:00。
    create 是昨日、now 是今日 → R1 不命中。
    gap from active 02:00 → 09:00 = 7h > 30min → R2 切。
    """
    create = datetime(2026, 5, 10, 23, 30, tzinfo=SHANGHAI)
    active = datetime(2026, 5, 11, 2, 0, tzinfo=SHANGHAI)
    now = datetime(2026, 5, 11, 9, 0, tzinfo=SHANGHAI)
    assert should_switch_session(active, create, now) is True


def test_should_switch_session_cross_day_short_grace_continue():
    """create 在 T-1 23:55,active 跨到 T0 00:05(gap 10min,now=T0 00:05 < 04:00)→ R3' 不切。"""
    create = datetime(2026, 5, 10, 23, 55, tzinfo=SHANGHAI)
    active = datetime(2026, 5, 11, 0, 5, tzinfo=SHANGHAI)
    now = datetime(2026, 5, 11, 0, 5, tzinfo=SHANGHAI)
    assert should_switch_session(active, create, now) is False


# ---- today_session_title 中文格式 ----

def test_today_session_title_cn_format():
    """中文标题格式:`周一 · 5月11日`。"""
    assert today_session_title(
        datetime(2026, 5, 11, 14, 30, tzinfo=SHANGHAI)
    ) == "周一 · 5月11日"


def test_today_session_title_natural_day_at_2am():
    """02:00 自然日边界:标题用当天日期(非昨日)。

    旧实现(boundary=4)会返回"昨日"。新实现(boundary=0)返回当天。
    这是 product-visible 的语义变化;测试钉死新行为防止回归。
    """
    # 2026-05-11 是周一
    assert today_session_title(
        datetime(2026, 5, 11, 2, 0, tzinfo=SHANGHAI)
    ) == "周一 · 5月11日"


def test_today_session_title_default_no_arg():
    """不传 now 参数不报错,返回今日格式字符串。"""
    title = today_session_title()
    assert "周" in title and "月" in title and "日" in title


# ---- logical_day 语义 ----

def test_logical_day_natural_day_boundary():
    """23:59:59 与次日 00:00 属不同自然日。"""
    d1 = logical_day(datetime(2026, 5, 11, 23, 59, 59, tzinfo=SHANGHAI))
    d2 = logical_day(datetime(2026, 5, 12, 0, 0, 0, tzinfo=SHANGHAI))
    assert d1 != d2


def test_logical_day_rejects_naive():
    """无时区 datetime → raise ValueError。"""
    with pytest.raises(ValueError, match="timezone-aware"):
        logical_day(datetime(2026, 5, 11, 14, 30))


# ---- 常量值 ----

def test_session_policy_constants():
    """常量值与计划一致。"""
    assert SESSION_DAY_BOUNDARY_HOUR == 0
    assert SESSION_CROSS_DAY_GRACE_MINUTES == 30
    assert SESSION_CROSS_DAY_HARD_CUT_HOUR == 4
