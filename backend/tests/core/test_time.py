"""core.time 纯函数测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.time import SHANGHAI, same_natural_day


class TestSameNaturalDay:
    """两个带时区的 datetime 落在同一 Shanghai 自然日历日。"""

    def test_same_instant(self):
        ts = datetime(2026, 6, 26, 12, 0, 0, tzinfo=SHANGHAI)
        assert same_natural_day(ts, ts) is True

    def test_same_day_different_times(self):
        a = datetime(2026, 6, 26, 0, 0, 0, tzinfo=SHANGHAI)
        b = datetime(2026, 6, 26, 23, 59, 59, tzinfo=SHANGHAI)
        assert same_natural_day(a, b) is True

    def test_cross_day_one_second_apart(self):
        a = datetime(2026, 6, 26, 23, 59, 59, tzinfo=SHANGHAI)
        b = datetime(2026, 6, 27, 0, 0, 0, tzinfo=SHANGHAI)
        assert same_natural_day(a, b) is False

    def test_cross_midnight(self):
        a = datetime(2026, 6, 26, 22, 0, 0, tzinfo=SHANGHAI)
        b = datetime(2026, 6, 27, 2, 0, 0, tzinfo=SHANGHAI)
        assert same_natural_day(a, b) is False

    def test_utc_inputs_converted_to_shanghai(self):
        # 2026-06-26 16:30 UTC = 2026-06-27 00:30 Shanghai(次日自然日)
        # 2026-06-26 23:00 Shanghai = 2026-06-26 23:00 Shanghai(当日自然日)
        # 两者分属不同自然日 → False
        a = datetime(2026, 6, 26, 16, 30, 0, tzinfo=UTC)
        b = datetime(2026, 6, 26, 23, 0, 0, tzinfo=SHANGHAI)
        assert same_natural_day(a, b) is False

    def test_naive_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            same_natural_day(datetime(2026, 6, 26, 12, 0), datetime(2026, 6, 26, 12, 0))
