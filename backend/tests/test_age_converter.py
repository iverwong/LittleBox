"""M4.8 B2 TDD：age ↔ birth_date 换算服务。"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.age_converter import age_to_birth_date, birth_date_to_age


class TestAgeToBirthDate:
    """age_to_birth_date(age, ref) 测试。"""

    @pytest.mark.parametrize("age,ref", [
        (3, date(2026, 4, 27)),
        (4, date(2026, 4, 27)),
        (12, date(2026, 4, 27)),
        (20, date(2026, 4, 27)),
        (21, date(2026, 4, 27)),
    ])
    def test_round_trip_via_ref(self, age: int, ref: date) -> None:
        """正向 round-trip：birth_date_to_age(age_to_birth_date(age, ref), ref) == age。"""
        bd = age_to_birth_date(age, ref)
        result = birth_date_to_age(bd, ref)
        assert result == age

    @pytest.mark.parametrize("age,ref", [
        # 普通日期
        (12, date(2026, 6, 15)),
        (20, date(2026, 12, 31)),   # 月底边界
        # 闰年 2-29 → 闰年（不触发兜底）
        (4, date(2024, 2, 29)),
    ])
    def test_round_trip_matrix(self, age: int, ref: date) -> None:
        """扩展 round-trip 矩阵：普通日 / 2-29 / 月底。"""
        bd = age_to_birth_date(age, ref)
        assert birth_date_to_age(bd, ref) == age

    def test_leap_year_fallback_triggered(self) -> None:
        """ref=2024-02-29, age=3 → date(2021, 2, 28)（越界检查放行 → date(2021, 2, 29) 失败 → except 分支）。"""
        result = age_to_birth_date(3, date(2024, 2, 29))
        assert result == date(2021, 2, 28)

    def test_leap_year_fallback_not_triggered(self) -> None:
        """ref=2024-02-29, age=4 → date(2020, 2, 29)（闰年→闰年，直接成功）。"""
        result = age_to_birth_date(4, date(2024, 2, 29))
        assert result == date(2020, 2, 29)

    @pytest.mark.parametrize("age", [-1, 0, 2, 22, 100])
    def test_out_of_range_raises_value_error(self, age: int) -> None:
        """age ∉ [3, 21] → ValueError，消息含 'age must be in [3, 21]'。"""
        with pytest.raises(ValueError, match=r"age must be in \[3, 21\]"):
            age_to_birth_date(age)


class TestBirthDateToAge:
    """birth_date_to_age(birth_date, ref) 测试。"""

    @pytest.mark.parametrize("ref,bd,expected", [
        # 钳位：raw=2 → 3
        (date(2026, 1, 1), date(2024, 1, 1), 3),
        # 钳位：raw=26 → 21
        (date(2026, 1, 1), date(2000, 1, 1), 21),
        # 边界：raw=3 → 3（不误伤合法值）
        (date(2026, 4, 27), date(2023, 4, 27), 3),
        # 边界：raw=21 → 21（不误伤合法值）
        (date(2026, 4, 27), date(2005, 4, 27), 21),
        # 普通：raw=12
        (date(2026, 4, 27), date(2014, 4, 27), 12),
        # 生日未到：当月之前，生日已过
        (date(2026, 6, 1), date(2022, 3, 15), 4),
    ])
    def test_birth_date_to_age_clamping(self, ref: date, bd: date, expected: int) -> None:
        """birth_date_to_age 钳位到 [3, 21]，不因极端 birth_date 抛异常。"""
        assert birth_date_to_age(bd, ref) == expected
