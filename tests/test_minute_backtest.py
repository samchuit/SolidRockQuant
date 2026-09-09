"""分钟线回测测试（合成 5 分钟 bar，验证日内时钟与撮合）."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from solidrock.agent.errors import ErrorCode, SolidRockError
from solidrock.backtest import BacktestConfig, BacktestEngine
from solidrock.backtest.costs import AShareCostModel
from solidrock.backtest.engine import BacktestResult
from solidrock.data.store import DataStore
from solidrock.strategy.base import Strategy
from tests.conftest import bars_frame

SYM = "510300.SH"


def zero_cost() -> AShareCostModel:
    return AShareCostModel(
        commission_rate=0.0,
        commission_min=0.0,
        stamp_duty_rate=0.0,
        transfer_fee_rate=0.0,
        slippage_bps=0.0,
    )


def minute_store(
    tmp_path, *, days: int = 2, bars_per_day: int = 8, base: float = 10.0, drift: float = 0.0
) -> DataStore:
    """合成 5 分钟 bar：每天 bars_per_day 根，第二日起价格每日 +1。"""
    store = DataStore(tmp_path / "m5")
    frames = []
    ticks_all = []
    for d in range(days):
        day0 = pd.Timestamp("2024-01-02") + pd.Timedelta(days=d)
        # 每日 4 小时（10:00-14:00 简化），bars_per_day 根
        ticks = pd.date_range(day0 + pd.Timedelta(hours=10), periods=bars_per_day, freq="5min")
        ticks_all.extend(ticks)
        closes = [base + d + drift * d + 0.1 * i for i in range(bars_per_day)]
        frames.append(
            bars_frame(
                SYM,
                ticks,
                closes,
                opens=[c - 0.05 for c in closes],
                highs=[c + 0.05 for c in closes],
                lows=[c - 0.08 for c in closes],
                pre_closes=[base + d - 0.05, *closes[:-1]],
            )
        )
    store.save_calendar(pd.DataFrame({"date": pd.date_range("2024-01-02", periods=days).normalize()}))
    store.save_bars(pd.concat(frames, ignore_index=True), freq="5m", source="synthetic")
    return store


class BuyOnce(Strategy):
    params = {"symbol": SYM, "percent": 1.0}

    def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
        ctx.universe = [str(self.params["symbol"])]

    def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
        symbol = str(self.params["symbol"])
        if ctx.position(symbol) == 0:
            ctx.order_target_percent(symbol, float(self.params["percent"]))


class TestMinuteBacktest:
    def _config(self, store: DataStore, start: str, end: str) -> BacktestConfig:
        return BacktestConfig(
            start=start,
            end=end,
            benchmark=None,
            cost_model=zero_cost(),
            log_experiment=False,
            max_position_weight=None,
            freq="5m",
            warmup_bars=16,
        )

    def test_buy_fills_next_bar(self, tmp_path) -> None:
        """分钟模式：t bar 出信号 → t+1 bar 开盘成交。"""
        store = minute_store(tmp_path, days=2, bars_per_day=8, base=10.0)
        result = BacktestEngine(BuyOnce, self._config(store, "2024-01-02", "2024-01-03"), store).run()
        assert len(result.trades) == 1
        trade = result.trades.iloc[0]
        assert trade["side"] == "buy"
        # 首 bar close=10.0 → 信号；次 bar open = 10.1 - 0.05 = 10.05
        assert trade["price"] == pytest.approx(10.05)

    def test_day_boundary_t_plus_one(self, tmp_path: Path) -> None:
        """T+1 按日界解锁：买入 bar 所在日不可卖，换日后可卖。"""
        store = minute_store(tmp_path, days=3, bars_per_day=8, base=10.0)

        class BuyThenSell(Strategy):
            params = {"symbol": SYM}

            def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
                ctx.universe = [str(self.params["symbol"])]

            def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
                symbol = str(self.params["symbol"])
                if ctx.position(symbol) == 0:
                    ctx.order(symbol, 100)
                elif ctx.now.date() == pd.Timestamp("2024-01-03").date():
                    ctx.order(symbol, -100)  # 次日卖出

        config = BacktestConfig(
            start="2024-01-02",
            end="2024-01-04",
            benchmark=None,
            cost_model=zero_cost(),
            log_experiment=False,
            max_position_weight=None,
            freq="5m",
            warmup_bars=16,
        )
        result = BacktestEngine(BuyThenSell(), config, store).run()
        sells = result.trades[result.trades["side"] == "sell"]
        # 卖出在次日（01-03）发生（T+1 换日后解锁）
        assert len(sells) >= 1
        assert sells.iloc[0]["date"].date() == pd.Timestamp("2024-01-03").date()

    def test_metrics_daily_resample(self, tmp_path) -> None:
        """分钟净值的指标按日重采样：年化口径与日频一致。"""
        store = minute_store(tmp_path, days=3, bars_per_day=8, base=10.0, drift=0.0)
        result = BacktestEngine(BuyOnce, self._config(store, "2024-01-02", "2024-01-04"), store).run()
        # nav 为 bar 级（3 天 × 8 bar = 24 根）
        assert len(result.nav) == 24
        # 指标按日重采样：total_return 来自日收盘，与 bar 级总收益一致
        nav_daily = result.nav["total"].groupby(pd.DatetimeIndex(result.nav.index).normalize()).last()
        expected = (nav_daily.iloc[-1] / nav_daily.iloc[0]) - 1
        assert result.metrics["total_return"] == pytest.approx(expected)

    def test_minute_futures_rejected(self, tmp_path: Path) -> None:
        store = minute_store(tmp_path, days=2, bars_per_day=8)

        class Fut(Strategy):
            def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
                ctx.universe = ["RB2505.SHFE"]

            def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
                pass

        config = self._config(store, "2024-01-02", "2024-01-03")
        with pytest.raises(SolidRockError) as exc_info:
            BacktestEngine(Fut, config, store).run()
        assert exc_info.value.code is ErrorCode.PARAM_INVALID

    def test_missing_minute_data(self, tmp_path: Path) -> None:
        store = minute_store(tmp_path, days=2, bars_per_day=8)

        class NoData(Strategy):
            def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
                ctx.universe = ["600519.SH"]

            def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
                pass

        with pytest.raises(SolidRockError) as exc_info:
            BacktestEngine(NoData, self._config(store, "2024-01-02", "2024-01-03"), store).run()
        assert exc_info.value.code is ErrorCode.NO_DATA

    def test_history_minute_timestamps(self, tmp_path: Path) -> None:
        """ctx.history 返回分钟时间戳且包含当前 bar。"""
        seen = {}

        class Probe(Strategy):
            params = {"symbol": SYM}

            def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
                ctx.universe = [str(self.params["symbol"])]

            def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
                if ctx.now == pd.Timestamp("2024-01-02 10:15:00"):
                    seen["close"] = ctx.history(SYM, 2, fields="close")[SYM].tolist()

        BacktestEngine(
            Probe, self._config(store := minute_store(tmp_path, days=2), "2024-01-02", "2024-01-03"), store
        ).run()
        assert seen["close"] == [pytest.approx(10.2), pytest.approx(10.3)]

    def test_result_type_annotation(self) -> None:
        assert BacktestResult is not None  # 冒烟
