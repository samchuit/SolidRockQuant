"""M3 单元测试：费用模型、组合核算、撮合规则."""

from __future__ import annotations

import pandas as pd
import pytest

from solidrock.agent.errors import SolidRockError
from solidrock.backtest.costs import AShareCostModel
from solidrock.backtest.matching import (
    ExecutionSimulator,
    Order,
    board_limit_ratio,
    round_half_up,
)
from solidrock.backtest.portfolio import Portfolio


def zero_cost() -> AShareCostModel:
    return AShareCostModel(
        commission_rate=0.0,
        commission_min=0.0,
        stamp_duty_rate=0.0,
        transfer_fee_rate=0.0,
        slippage_bps=0.0,
    )


def make_portfolio(cash: float = 1_000_000.0) -> Portfolio:
    return Portfolio(initial_cash=cash, cash=cash)


def make_bar(open_: float, close: float, pre_close: float) -> pd.Series:
    return pd.Series(
        {
            "open": open_,
            "high": max(open_, close) * 1.01,
            "low": min(open_, close) * 0.99,
            "close": close,
            "pre_close": pre_close,
        }
    )


class TestCosts:
    def test_buy_fees(self) -> None:
        model = AShareCostModel(
            commission_rate=2.5e-4, commission_min=5.0, stamp_duty_rate=5e-4, transfer_fee_rate=1e-5, slippage_bps=0.0
        )
        value = 100_000.0
        fees = model.fees(value, "buy")
        assert fees == pytest.approx(value * 2.5e-4 + value * 1e-5)

    def test_sell_fees_include_stamp(self) -> None:
        model = AShareCostModel(
            commission_rate=2.5e-4, commission_min=5.0, stamp_duty_rate=5e-4, transfer_fee_rate=1e-5, slippage_bps=0.0
        )
        value = 100_000.0
        fees = model.fees(value, "sell")
        assert fees == pytest.approx(value * 2.5e-4 + value * 5e-4 + value * 1e-5)

    def test_min_commission(self) -> None:
        model = AShareCostModel(
            commission_rate=2.5e-4, commission_min=5.0, stamp_duty_rate=0.0, transfer_fee_rate=0.0, slippage_bps=0.0
        )
        assert model.fees(1000.0, "buy") == pytest.approx(5.0)  # 0.25 → 最低 5 元

    def test_slippage_direction(self) -> None:
        model = AShareCostModel(
            commission_rate=0, commission_min=0, stamp_duty_rate=0, transfer_fee_rate=0, slippage_bps=10.0
        )
        assert model.slipped_price(10.0, "buy") == pytest.approx(10.01)
        assert model.slipped_price(10.0, "sell") == pytest.approx(9.99)


class TestPortfolio:
    def test_buy_sell_accounting(self) -> None:
        p = make_portfolio()
        p.buy("510300.SH", 1000, 10.0, 5.0)
        assert p.cash == pytest.approx(1_000_000 - 10_005)
        pos = p.position("510300.SH")
        assert pos.shares == 1000
        assert pos.avg_cost == pytest.approx(10.005)  # 费用摊入
        assert pos.available == 0  # T+1 当日不可卖
        p.release_available()  # 次日解锁
        trade = p.sell("510300.SH", 1000, 11.0, 6.0)
        assert p.cash == pytest.approx(1_000_000 - 10_005 + 11_000 - 6)
        assert trade.pnl == pytest.approx((11.0 - 10.005) * 1000 - 6)
        assert "510300.SH" not in p.positions

    def test_release_t_plus_one(self) -> None:
        p = make_portfolio()
        p.buy("510300.SH", 500, 10.0, 0.0)
        with pytest.raises(SolidRockError):
            p.sell("510300.SH", 500, 10.0, 0.0)  # 当日不可卖
        p.release_available()
        p.sell("510300.SH", 500, 10.0, 0.0)  # 次日可卖
        assert p.position("510300.SH").shares == 0

    def test_corporate_action_value_conserving(self) -> None:
        p = make_portfolio()
        p.buy("510300.SH", 1000, 10.0, 0.0)
        p.release_available()
        # 10送10：因子翻倍 → 持仓翻倍、成本减半（价格减半由引擎的 last_prices 同步）
        p.apply_corporate_action("510300.SH", 2.0)
        pos = p.position("510300.SH")
        assert pos.shares == 2000
        assert pos.avg_cost == pytest.approx(5.0)
        assert pos.available == 2000
        pos.last_price = 5.0  # 引擎侧用新 bar 的 raw 收盘价刷新
        assert pos.market_value == pytest.approx(10_000.0)  # 价值守恒

    def test_insufficient_cash_rejected(self) -> None:
        p = make_portfolio(cash=100.0)
        with pytest.raises(SolidRockError):
            p.buy("510300.SH", 100, 10.0, 0.0)


class TestLimitRules:
    def test_board_ratios(self) -> None:
        assert board_limit_ratio("600519.SH") == 0.10
        assert board_limit_ratio("300750.SZ") == 0.20
        assert board_limit_ratio("688981.SH") == 0.20
        assert board_limit_ratio("830799.BJ") == 0.30
        assert board_limit_ratio("000001.SZ", overrides={"000001.SZ": 0.05}) == 0.05

    def test_round_half_up(self) -> None:
        assert round_half_up(9.875) == 9.88  # 银行家舍入会得到 9.88 之外的结果
        assert round_half_up(10.854) == 10.85
        assert round_half_up(10.855) == 10.86


class TestSimulateFill:
    def test_buy_at_open_next_open(self) -> None:
        sim = ExecutionSimulator(zero_cost(), mode="next_open")
        order = Order("510300.SH", "buy", 1000)
        result = sim.simulate_fill(order, make_bar(10.0, 10.5, 10.0), make_portfolio())
        assert result.filled_qty == 1000
        assert result.price == pytest.approx(10.0)

    def test_limit_up_blocks_buy(self) -> None:
        sim = ExecutionSimulator(zero_cost(), mode="next_open")
        # pre_close=10 → 涨停价 11.0；open 一字涨停
        result = sim.simulate_fill(Order("510300.SH", "buy", 1000), make_bar(11.0, 11.0, 10.0), make_portfolio())
        assert result.rejected == "LIMIT_UP"

    def test_limit_down_blocks_sell(self) -> None:
        sim = ExecutionSimulator(zero_cost(), mode="next_open")
        result = sim.simulate_fill(Order("510300.SH", "sell", 1000), make_bar(9.0, 9.0, 10.0), make_portfolio())
        assert result.rejected == "LIMIT_DOWN"

    def test_limit_up_allows_sell(self) -> None:
        sim = ExecutionSimulator(zero_cost(), mode="next_open")
        p = make_portfolio()
        p.buy("510300.SH", 1000, 9.0, 0.0)
        p.release_available()
        result = sim.simulate_fill(Order("510300.SH", "sell", 1000), make_bar(11.0, 11.0, 10.0), p)
        assert result.rejected is None

    def test_t_plus_one_rejection(self) -> None:
        sim = ExecutionSimulator(zero_cost(), mode="same_close")
        p = make_portfolio()
        p.buy("510300.SH", 500, 10.0, 0.0)  # 当日买入
        result = sim.simulate_fill(Order("510300.SH", "sell", 500), make_bar(10.0, 10.0, 10.0), p)
        assert result.rejected == "T_PLUS_ONE"

    def test_lot_rounding(self) -> None:
        sim = ExecutionSimulator(zero_cost(), mode="next_open")
        result = sim.simulate_fill(Order("510300.SH", "buy", 350), make_bar(10.0, 10.0, 10.0), make_portfolio())
        assert result.filled_qty == 300

    def test_lot_disabled(self) -> None:
        sim = ExecutionSimulator(zero_cost(), mode="next_open", round_lot=False)
        result = sim.simulate_fill(Order("510300.SH", "buy", 350), make_bar(10.0, 10.0, 10.0), make_portfolio())
        assert result.filled_qty == 350

    def test_cash_cap(self) -> None:
        sim = ExecutionSimulator(zero_cost(), mode="next_open")
        p = make_portfolio(cash=1000.0)  # 只够 100 股
        result = sim.simulate_fill(Order("510300.SH", "buy", 500), make_bar(10.0, 10.0, 10.0), p)
        assert result.filled_qty == 100

    def test_insufficient_cash_rejects(self) -> None:
        sim = ExecutionSimulator(zero_cost(), mode="next_open")
        p = make_portfolio(cash=500.0)  # 一手都买不起
        result = sim.simulate_fill(Order("510300.SH", "buy", 100), make_bar(10.0, 10.0, 10.0), p)
        assert result.rejected == "INSUFFICIENT_CASH"

    def test_same_close_ref_price(self) -> None:
        sim = ExecutionSimulator(zero_cost(), mode="same_close")
        result = sim.simulate_fill(Order("510300.SH", "buy", 100), make_bar(10.0, 10.8, 10.0), make_portfolio())
        assert result.price == pytest.approx(10.8)  # 收盘成交
