"""期货回测测试：合约规格、换月复权、保证金、双向持仓、平今费率、到期强平."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from solidrock.backtest import BacktestConfig, BacktestEngine
from solidrock.backtest.costs import FuturesCostModel
from solidrock.data.futures import (
    contract_expiry,
    get_contract_spec,
    roll_adjust_continuous,
)
from solidrock.data.store import DataStore
from solidrock.strategy.base import Strategy
from tests.conftest import bars_frame, make_market_store

SYM = "RB2505.SHFE"


def zero_futures_cost() -> FuturesCostModel:
    return FuturesCostModel(open_fee_rate=0.0, close_fee_rate=0.0, close_today_fee_rate=0.0, slippage_bps=0.0)


class TestSpecs:
    def test_default_spec(self) -> None:
        spec = get_contract_spec("RB2505.SHFE")
        assert spec.multiplier == 10
        assert spec.margin_rate == pytest.approx(0.13)

    def test_override_by_product_and_symbol(self) -> None:
        spec = get_contract_spec("RB2510.SHFE", {"RB": {"margin_rate": 0.2}})
        assert spec.margin_rate == pytest.approx(0.2)
        spec = get_contract_spec("RB2510.SHFE", {"RB2510.SHFE": {"multiplier": 5}})
        assert spec.multiplier == 5

    def test_unknown_product_fallback(self) -> None:
        spec = get_contract_spec("ZZ2601.DCE")
        assert spec.multiplier == 10  # 保守默认值

    def test_stock_rejected(self) -> None:
        from solidrock.agent.errors import SolidRockError

        with pytest.raises(SolidRockError):
            get_contract_spec("000001.SZ")


class TestExpiry:
    def test_commodity(self) -> None:
        assert contract_expiry("RB2505.SHFE") == pd.Timestamp("2025-05-15")

    def test_czce_three_digit(self) -> None:
        assert contract_expiry("TA505.CZCE") == pd.Timestamp("2025-05-15")

    def test_cfe_third_friday(self) -> None:
        # 2024-12 的第三个周五是 12-20
        assert contract_expiry("IF2412.CFE") == pd.Timestamp("2024-12-20")

    def test_continuous_none(self) -> None:
        assert contract_expiry("RB.SHFE") is None


class TestRollAdjust:
    def test_jump_gets_adjusted(self) -> None:
        dates = pd.bdate_range("2024-01-02", periods=6)
        df = pd.DataFrame(
            {
                "date": dates,
                "close": [100.0, 101.0, 102.0, 105.0, 105.5, 106.0],  # 102→105 为换月跳变(+2.9%)?
            }
        )
        # 用明显跳变：102 → 110（+7.8% > 5% 阈值）
        df.loc[df.index[3], "close"] = 110.0
        out = roll_adjust_continuous(df, threshold=0.05)
        hfq = out["close"] * out["adj_factor"]
        # 换月点之后 hfq 连续：跳变日 hfq == 前一日 hfq（无收益跳变）
        assert hfq.iloc[3] == pytest.approx(hfq.iloc[2], rel=1e-9)
        assert out["adj_factor"].iloc[0] == pytest.approx(1.0)
        # 换月后因子保持不变（无新跳变）
        assert out["adj_factor"].iloc[5] == pytest.approx(102.0 / 110.0)


class TestFuturesCost:
    def test_trade_fees_split(self) -> None:
        fcm = FuturesCostModel(open_fee_rate=1e-4, close_fee_rate=1e-4, close_today_fee_rate=5e-4, slippage_bps=0)
        # 开仓 5000
        assert fcm.trade_fees(open_value=5000.0) == pytest.approx(0.5)
        # 平昨 5000
        assert fcm.trade_fees(close_value=5000.0) == pytest.approx(0.5)
        # 平今 5000（全部按平今费率）
        assert fcm.trade_fees(close_value=5000.0, close_today_value=5000.0) == pytest.approx(2.5)
        # 混合：平昨 3000 + 平今 2000
        fees = fcm.trade_fees(close_value=5000.0, close_today_value=2000.0)
        assert fees == pytest.approx(3000 * 1e-4 + 2000 * 5e-4)


class TestPortfolioFutures:
    def test_open_close_long_pnl(self) -> None:
        from solidrock.backtest.portfolio import Portfolio

        p = Portfolio(initial_cash=1_000_000.0, cash=1_000_000.0)
        p.multipliers = {"RB2505.SHFE": 10}
        p.apply_fill("RB2505.SHFE", 10, 100.0, 5.0)  # 开多 10 手：现金流 = -价值 - 费用
        assert p.cash == pytest.approx(1_000_000 - 10 * 100 * 10 - 5)
        assert p.position("RB2505.SHFE").shares == 10
        pnl = p.apply_fill("RB2505.SHFE", -10, 105.0, 5.0)  # 平多：现金流 = +价值 - 费用
        assert pnl == pytest.approx((105 - 100) * 10 * 10 - 5)
        # 现金净变化 = 毛利 500 - 开仓费 5 - 平仓费 5 = 490；与两次 pnl 记录之和一致
        assert p.cash == pytest.approx(1_000_000 + 490)
        assert p.realized_pnl == pytest.approx(p.cash - 1_000_000.0)

    def test_short_profit_when_price_falls(self) -> None:
        from solidrock.backtest.portfolio import Portfolio

        p = Portfolio(initial_cash=1_000_000.0, cash=1_000_000.0)
        p.multipliers = {"RB2505.SHFE": 10}
        p.apply_fill("RB2505.SHFE", -10, 100.0, 0.0)  # 开空
        assert p.position("RB2505.SHFE").shares == -10
        assert p.market_value({"RB2505.SHFE": 95.0}) == pytest.approx(-9500.0)  # 空头浮盈
        pnl = p.apply_fill("RB2505.SHFE", 10, 95.0, 0.0)  # 买入回补
        assert pnl == pytest.approx((100 - 95) * 10 * 10)

    def test_flip(self) -> None:
        from solidrock.backtest.portfolio import Portfolio

        p = Portfolio(initial_cash=1_000_000.0, cash=1_000_000.0)
        p.multipliers = {"RB2505.SHFE": 10}
        p.apply_fill("RB2505.SHFE", 10, 100.0, 0.0)  # 多 10
        p.apply_fill("RB2505.SHFE", -25, 110.0, 0.0)  # 平 10 + 开空 15
        pos = p.position("RB2505.SHFE")
        assert pos.shares == -15
        assert pos.avg_cost == pytest.approx(110.0)
        assert p.realized_pnl == pytest.approx((110 - 100) * 10 * 10)


class TestFuturesMatching:
    def make_portfolio(self, cash: float = 1_000_000.0):
        from solidrock.backtest.portfolio import Portfolio

        return Portfolio(initial_cash=cash, cash=cash)

    def make_bar(self, open_: float, close: float, pre_close: float) -> pd.Series:
        return pd.Series(
            {
                "open": open_,
                "high": max(open_, close) * 1.01,
                "low": min(open_, close) * 0.99,
                "close": close,
                "pre_close": pre_close,
            }
        )

    def spec(self, **kwargs) -> object:
        from solidrock.data.futures import get_contract_spec

        return get_contract_spec("RB2505.SHFE")

    def _sim(self, **rates) -> object:
        from solidrock.backtest.matching import ExecutionSimulator

        return ExecutionSimulator(
            zero_futures_cost(), futures_cost_model=FuturesCostModel(**{"slippage_bps": 0.0, **rates})
        )

    def test_integer_lots(self) -> None:
        from solidrock.backtest.matching import Order

        result = self._sim().simulate_futures_fill(
            Order(SYM, "buy", 10.7), self.make_bar(100.0, 100.0, 100.0), self.make_portfolio(), self.spec()
        )
        assert result.filled_qty == 10

    def test_limit_up_blocked_by_spec_ratio(self) -> None:
        from solidrock.backtest.matching import Order

        # RB 默认 ±7%：pre_close=100 → 涨停 107
        result = self._sim().simulate_futures_fill(
            Order(SYM, "buy", 5), self.make_bar(107.0, 107.0, 100.0), self.make_portfolio(), self.spec()
        )
        assert result.rejected == "LIMIT_UP"

    def test_margin_cap_reduces_lots(self) -> None:
        from solidrock.backtest.matching import Order

        sim = self._sim()
        p = self.make_portfolio(cash=1400.0)  # 10 手×100×10×0.13 = 1300 保证金 + 费用 → 只够少量
        result = sim.simulate_futures_fill(
            Order(SYM, "buy", 100), self.make_bar(100.0, 100.0, 100.0), p, self.spec(), margin_headroom=1400.0
        )
        assert result.filled_qty < 100
        assert result.open_qty == result.filled_qty

    def test_pure_close_needs_no_margin(self) -> None:
        from solidrock.backtest.matching import Order

        sim = self._sim()
        p = self.make_portfolio(cash=0.0)  # 没现金：纯平仓也应放行（费用为 0）
        p.apply_fill(SYM, 10, 100.0, 0.0)  # 先持有多头 10 手
        result = sim.simulate_futures_fill(
            Order(SYM, "sell", 10), self.make_bar(105.0, 105.0, 100.0), p, self.spec(), margin_headroom=0.0
        )
        assert result.rejected is None
        assert result.close_qty == 10


class TestFuturesEngine:
    def _store(self, tmp_path: Path, days: int, base: float, drift: float) -> DataStore:
        return make_market_store(tmp_path, symbols=(SYM,), days=days, base=base, drift=drift)

    def _config(self, store: DataStore, **overrides) -> BacktestConfig:
        defaults: dict = {
            "start": "2024-01-02",
            "end": "2024-02-15",
            "benchmark": None,
            "cost_model": zero_futures_cost(),
            "futures_cost_model": zero_futures_cost(),
            "log_experiment": False,
            "max_position_weight": None,
        }
        defaults.update(overrides)
        return BacktestConfig(**defaults)

    def test_short_profit(self, tmp_path: Path) -> None:
        """做空 + 价格下跌：净值上升，盈亏 = (开仓价-现价)×手数×乘数。"""
        store = self._store(tmp_path, days=10, base=100.0, drift=-0.5)

        class ShortStart(Strategy):
            params = {"symbol": SYM}

            def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
                ctx.universe = [str(self.params["symbol"])]

            def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
                symbol = str(self.params["symbol"])
                if ctx.position(symbol) == 0:
                    ctx.order(symbol, -10)  # 开空 10 手

        result = BacktestEngine(ShortStart(), self._config(store), store).run()
        assert len(result.trades) == 1
        trade = result.trades.iloc[0]
        assert trade["side"] == "sell"
        assert trade["qty"] == 10
        # day1 开盘 = day1 收盘 × 0.99 = 99.5×0.99；持有至末日，浮动盈亏计入净值
        entry = 99.5 * 0.99
        last_close = 100.0 - 0.5 * 9  # 95.5
        floating = (entry - last_close) * 10 * 10
        assert result.nav["total"].iloc[-1] == pytest.approx(1_000_000 + floating)

    def test_margin_cap(self, tmp_path: Path) -> None:
        """保证金不足时开仓手数被下调。"""
        store = self._store(tmp_path, days=10, base=100.0, drift=0.0)

        class HugeShort(Strategy):
            params = {"symbol": SYM}

            def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
                ctx.universe = [str(self.params["symbol"])]

            def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
                symbol = str(self.params["symbol"])
                if ctx.position(symbol) == 0:
                    ctx.order(symbol, -10_000)  # 需要保证金 10000×99×10×0.13 ≈ 128.7 万 > 100 万

        result = BacktestEngine(HugeShort(), self._config(store), store).run()
        trade = result.trades.iloc[0]
        # headroom 100 万 / 每手保证金 99×10×0.13=128.7 → 7770 手
        assert trade["qty"] == 7770

    def test_expiry_force_close(self, tmp_path: Path) -> None:
        """到期日强平：持仓被了结，此后的策略订单收到 EXPIRED 拒单。"""
        store = DataStore(tmp_path / "exp")
        dates = pd.bdate_range("2025-05-12", periods=6)  # 05-12..05-19，到期日 05-15
        store.save_calendar(pd.DataFrame({"date": dates}))
        store.save_bars(bars_frame(SYM, dates, [3000.0] * 6), source="synthetic")

        class AlwaysShort(Strategy):
            params = {"symbol": SYM}

            def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
                ctx.universe = [str(self.params["symbol"])]

            def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
                symbol = str(self.params["symbol"])
                if ctx.position(symbol) == 0:
                    ctx.order(symbol, -5)

        config = self._config(store, start="2025-05-12", end="2025-05-19")
        result = BacktestEngine(AlwaysShort(), config, store).run()
        assert result.final_positions == {}  # 到期强平后空仓
        assert "EXPIRED" in set(result.rejections["code"])  # 到期后继续做空被拒

    def test_close_today_fee(self, tmp_path: Path) -> None:
        """same_close 模式同日开平：平仓部分按平今费率计费。"""
        store = self._store(tmp_path, days=6, base=100.0, drift=0.0)

        class OpenAndClose(Strategy):
            params = {"symbol": SYM}

            def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
                ctx.universe = [str(self.params["symbol"])]
                self.done = False

            def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
                symbol = str(self.params["symbol"])
                if not self.done and ctx.position(symbol) == 0:
                    ctx.order(symbol, 5)  # 开多 5 手
                    ctx.order(symbol, -5)  # 同日平仓 → 平今
                    self.done = True

        fcm = FuturesCostModel(open_fee_rate=1e-4, close_fee_rate=1e-4, close_today_fee_rate=5e-4, slippage_bps=0)
        config = self._config(store, execution="same_close", futures_cost_model=fcm)
        result = BacktestEngine(OpenAndClose(), config, store).run()
        assert len(result.trades) == 2
        total_fees = result.trades["fees"].sum()
        # 每笔价值 = 5 手 × 100 × 10 = 5000：开仓 0.5 + 平今 2.5
        assert total_fees == pytest.approx(5000 * 1e-4 + 5000 * 5e-4)
