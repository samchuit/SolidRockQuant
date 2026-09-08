"""向量化回测通道测试."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from solidrock.agent.errors import SolidRockError
from solidrock.backtest.vectorized import vectorized_backtest, weights_from_factor


def _panel(dates: pd.DatetimeIndex, closes_by_symbol: dict[str, list[float]]) -> pd.DataFrame:
    return pd.DataFrame(closes_by_symbol, index=dates)


class TestVectorized:
    def test_constant_weights_math(self) -> None:
        dates = pd.bdate_range("2024-01-02", periods=5)
        close = _panel(dates, {"A": [100.0, 101, 102, 103, 104], "B": [100.0, 99, 98, 97, 96]})
        weights = pd.DataFrame({"A": 0.5, "B": -0.5}, index=dates)  # 多A空B，市场中性
        result = vectorized_backtest(weights, close, fee_rate=0.0)
        # 每日组合收益 = 0.5×rA + (-0.5)×rB（首日无收益）
        assert result.port_returns.iloc[1] == pytest.approx(0.5 * 0.01 - 0.5 * (-0.01))
        # 期望净值独立重算（fill 只作用于收益，不能把首日 (1+NaN) 置 0）
        daily = (0.5 * close["A"].pct_change() - 0.5 * close["B"].pct_change()).fillna(0)
        expected = (1 + daily).cumprod()
        assert result.nav.iloc[-1] == pytest.approx(expected.iloc[-1], rel=1e-10)

    def test_zero_turnover_no_fee_drag(self) -> None:
        dates = pd.bdate_range("2024-01-02", periods=5)
        close = _panel(dates, {"A": [100.0, 101, 102, 103, 104], "B": [100.0, 99, 98, 97, 96]})
        weights = pd.DataFrame({"A": 0.5, "B": -0.5}, index=dates)
        with_fee = vectorized_backtest(weights, close, fee_rate=5e-3)
        without = vectorized_backtest(weights, close, fee_rate=0.0)
        # 建仓换手发生在权重生效首日（t1）：|0.5-0|+|(-0.5)-0| 再除 2 = 0.25
        assert with_fee.turnover.iloc[0] == pytest.approx(0.0)
        assert with_fee.turnover.iloc[1] == pytest.approx(0.5)
        assert (with_fee.turnover.iloc[2:] == 0).all()
        # 费用拖累：净值降低，降幅 ≈ 建仓费 0.5×5e-3=0.25%（在复利序列中结算）
        assert without.nav.iloc[-1] - with_fee.nav.iloc[-1] == pytest.approx(1.0406184 * 0.0025 / 1.01, rel=1e-3)

    def test_no_lookahead(self) -> None:
        """只有最后一日才有权重 → 滞后一期后无收益，净值恒为 1。"""
        dates = pd.bdate_range("2024-01-02", periods=5)
        close = _panel(dates, {"A": [100.0, 101, 102, 103, 104]})
        weights = pd.DataFrame(0.0, index=dates, columns=["A"])
        weights.iloc[-1] = 1.0  # 最后一天才给出满仓信号
        result = vectorized_backtest(weights, close, fee_rate=0.0)
        assert result.nav.iloc[-1] == pytest.approx(1.0)  # 信号滞后一期，落在数据之外

    def test_fee_reduces_when_rebalancing(self) -> None:
        dates = pd.bdate_range("2024-01-02", periods=5)
        close = _panel(dates, {"A": [100.0, 100, 100, 100, 100]})
        weights = pd.DataFrame({"A": [0.0, 1.0, 1.0, 0.0, 0.0]}, index=dates)  # 有换手
        with_fee = vectorized_backtest(weights, close, fee_rate=1e-2)
        without = vectorized_backtest(weights, close, fee_rate=0.0)
        assert with_fee.nav.iloc[-1] < without.nav.iloc[-1]

    def test_mismatched_symbols_rejected(self) -> None:
        dates = pd.bdate_range("2024-01-02", periods=3)
        weights = pd.DataFrame({"X": [1.0] * 3}, index=dates)
        close = _panel(dates, {"A": [100.0] * 3})
        with pytest.raises(SolidRockError):
            vectorized_backtest(weights, close)


class TestWeightsFromFactor:
    def test_top_bottom_selection(self) -> None:
        dates = pd.bdate_range("2024-01-02", periods=2)
        values = pd.DataFrame(
            {"a": [1.0, 1.0], "b": [2.0, 2.0], "c": [3.0, 3.0], "d": [4.0, 4.0], "e": [5.0, 5.0]},
            index=dates,
        )
        # 5 只、top=bottom=0.2 → 按百分位（含边界）：多头 {d(0.8), e(1.0)}，空头 {a(0.2)}
        w = weights_from_factor(values, top=0.2, bottom=0.2, gross=1.0)
        row = w.iloc[0]
        assert row["e"] == pytest.approx(0.25)
        assert row["d"] == pytest.approx(0.25)
        assert row["a"] == pytest.approx(-0.5)
        assert row[["b", "c"]].abs().sum() == 0
        assert row.abs().sum() == pytest.approx(1.0)  # gross 归一

    def test_insufficient_cross_section_nan(self) -> None:
        dates = pd.bdate_range("2024-01-02", periods=2)
        values = pd.DataFrame({"a": [1.0, np.nan], "b": [2.0, np.nan]}, index=dates)
        w = weights_from_factor(values, top=0.2, bottom=0.2)  # min_stocks 默认 3
        # 截面有效样本不足 → 全 NaN
        assert w.iloc[0].isna().all()
        assert w.iloc[1].isna().all()

    def test_invalid_quantiles(self) -> None:
        dates = pd.bdate_range("2024-01-02", periods=1)
        values = pd.DataFrame({"a": [1.0]}, index=dates)
        with pytest.raises(SolidRockError):
            weights_from_factor(values, top=0.6, bottom=0.6)  # top+bottom > 1
