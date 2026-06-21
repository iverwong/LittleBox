"""M10 TDD:SensitivityConfig schema 边界与默认值。

测试函数级 docstring 用 Given / When / Then。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.accounts.schemas import SensitivityConfig


class TestSensitivityConfigDefaults:
    """未传任何维度时,默认全部为 5(正常关注)。"""

    def test_all_dims_default_to_5(self) -> None:
        """Given 构造 SensitivityConfig 不传任何字段
        When 实例化
        Then 6 个维度全部为 5。
        """
        cfg = SensitivityConfig()
        assert cfg.emotional == 5
        assert cfg.social == 5
        assert cfg.values == 5
        assert cfg.boundaries == 5
        assert cfg.academic == 5
        assert cfg.lifestyle == 5


class TestSensitivityConfigBoundaries:
    """边界值 1 与 9 合法,0 与 10 触发 ValidationError。"""

    def test_min_boundary_1_legal(self) -> None:
        """Given 把所有维度设为 1
        When 实例化
        Then 无异常,值保留为 1。
        """
        cfg = SensitivityConfig(
            emotional=1, social=1, values=1, boundaries=1, academic=1, lifestyle=1
        )
        assert cfg.emotional == 1
        assert cfg.lifestyle == 1

    def test_max_boundary_9_legal(self) -> None:
        """Given 把所有维度设为 9
        When 实例化
        Then 无异常,值保留为 9。
        """
        cfg = SensitivityConfig(
            emotional=9, social=9, values=9, boundaries=9, academic=9, lifestyle=9
        )
        assert cfg.emotional == 9
        assert cfg.lifestyle == 9

    @pytest.mark.parametrize("value", [0, -1, 10, 100])
    def test_out_of_range_rejected(self, value: int) -> None:
        """Given 任一维度传入 `[1, 9]` 范围外的值(0 / -1 / 10 / 100)
        When 实例化
        Then 抛 ValidationError。
        """
        with pytest.raises(ValidationError):
            SensitivityConfig(emotional=value)


class TestSensitivityConfigPartialOverride:
    """部分字段覆盖:未传字段保留默认 5。"""

    def test_partial_override_keeps_defaults(self) -> None:
        """Given 只覆盖 emotional=8,其它维度不传
        When 实例化
        Then emotional=8,其它维度均为 5。
        """
        cfg = SensitivityConfig(emotional=8)
        assert cfg.emotional == 8
        assert cfg.social == 5
        assert cfg.values == 5
        assert cfg.boundaries == 5
        assert cfg.academic == 5
        assert cfg.lifestyle == 5