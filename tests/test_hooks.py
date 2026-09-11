"""P2.4 测试：盘前/盘后钩子（on_market_open / on_market_close）+ trigger_times 定时触发."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from solidrock.backtest import BacktestConfig, BacktestEngine
from solidrock.backtest.costs import AShareCostModel
from solidrock.data.store import DataStore
from solidrock.strategy.base import Strategy
from tests.conftest import bars_frame, make_market_store

SYM = "510300.SH"


def zero_cost() -> AShareCostModel:
    return AShareCostModel(
        commission_rate=0.0,
        commission_min=0.0,
        stamp_duty_rate=0.0,
        transfer_fee_rate=0.0,
        slippage_bps=0.0,
    )


class HookRecorder(Strategy):
    """记录全部钩子调用序列：[(hook, timestamp), ...]。"""

    params: dict[str, Any] = {}

    def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
        ctx.universe = [SYM]
        self.calls: list[tuple[str, pd.Timestamp]] = []

    def on_market_open(self, ctx) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(("open", ctx.now))

    def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(("signal", ctx.now))

    def on_market_close(self, ctx) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(("close", ctx.now))


class TestDailyHooks:
    def test_hooks_fire_once_per_day_in_order(self, tmp_path: Path) -> None:
        store = make_market_store(tmp_path, symbols=(SYM,), days=3, benchmark=None)
        strat = HookRecorder()
        cfg = BacktestConfig(
            start="2024-01-02",
            end="2024-01-04",
            benchmark=None,
            cost_model=zero_cost(),
            log_experiment=False,
        )
        BacktestEngine(strat, cfg, store).run()
        assert len(strat.calls) == 9  # 3 天 × (open, signal, close)
        kinds = [k for k, _ in strat.calls]
        assert kinds == ["open", "signal", "close"] * 3
        # 时间戳：open/signal/close 同日
        assert strat.calls[0][1] == pd.Timestamp("2024-01-02")
        assert strat.calls[8][1] == pd.Timestamp("2024-01-04")


def minute_store(tmp_path: Path, *, days: int = 2, bars_per_day: int = 4, start_hour: int = 10) -> DataStore:
    """合成 5 分钟 bar：每天 bars_per_day 根（10:00 起）。"""
    store = DataStore(tmp_path / "m5")
    frames = []
    for d in range(days):
        day0 = pd.Timestamp("2024-01-02") + pd.Timedelta(days=d)
        ticks = pd.date_range(day0 + pd.Timedelta(hours=start_hour), periods=bars_per_day, freq="5min")
        closes = [10.0 + 0.1 * i for i in range(bars_per_day)]
        frames.append(
            bars_frame(
                SYM,
                ticks,
                closes,
                opens=[c - 0.05 for c in closes],
                highs=[c + 0.05 for c in closes],
                lows=[c - 0.08 for c in closes],
                pre_closes=[10.0 - 0.05, *closes[:-1]],
            )
        )
    store.save_calendar(pd.DataFrame({"date": pd.date_range("2024-01-02", periods=days).normalize()}))
    store.save_bars(pd.concat(frames, ignore_index=True), freq="5m", source="synthetic")
    return store


def minute_config(**overrides: Any) -> BacktestConfig:
    base: dict[str, Any] = {
        "start": "2024-01-02",
        "end": "2024-01-03",
        "benchmark": None,
        "cost_model": zero_cost(),
        "log_experiment": False,
        "max_position_weight": None,
        "freq": "5m",
        "warmup_bars": 16,
    }
    base.update(overrides)
    return BacktestConfig(**base)


class TestMinuteHooks:
    def test_hooks_on_first_and_last_bar(self, tmp_path: Path) -> None:
        store = minute_store(tmp_path, days=2, bars_per_day=4)
        strat = HookRecorder()
        BacktestEngine(strat, minute_config(), store).run()
        assert len(strat.calls) == 12  # 2 天 × (open + 4×signal + close)
        kinds = [k for k, _ in strat.calls]
        assert kinds == ["open", "signal", "signal", "signal", "signal", "close"] * 2
        assert strat.calls[0][1] == pd.Timestamp("2024-01-02 10:00:00")
        assert strat.calls[5][1] == pd.Timestamp("2024-01-02 10:15:00")
        assert strat.calls[6][1] == pd.Timestamp("2024-01-03 10:00:00")

    def test_trigger_times(self, tmp_path: Path) -> None:
        store = minute_store(tmp_path, days=2, bars_per_day=4)
        strat = HookRecorder()
        BacktestEngine(strat, minute_config(trigger_times=["10:15"]), store).run()
        # 每天：open(10:00) + signal(10:15) + close(10:15)
        assert strat.calls == [
            ("open", pd.Timestamp("2024-01-02 10:00:00")),
            ("signal", pd.Timestamp("2024-01-02 10:15:00")),
            ("close", pd.Timestamp("2024-01-02 10:15:00")),
            ("open", pd.Timestamp("2024-01-03 10:00:00")),
            ("signal", pd.Timestamp("2024-01-03 10:15:00")),
            ("close", pd.Timestamp("2024-01-03 10:15:00")),
        ]

    def test_trigger_times_no_match_skips_signal(self, tmp_path: Path) -> None:
        store = minute_store(tmp_path, days=1, bars_per_day=4)
        strat = HookRecorder()
        BacktestEngine(strat, minute_config(end="2024-01-02", trigger_times=["09:31"]), store).run()
        # bar 时间戳为 10:00-10:15，无匹配 → 只有 open/close，无 signal
        assert [k for k, _ in strat.calls] == ["open", "close"]

    def test_trigger_times_ignored_on_daily(self, tmp_path: Path) -> None:
        store = make_market_store(tmp_path, symbols=(SYM,), days=2, benchmark=None)
        strat = HookRecorder()
        cfg = BacktestConfig(
            start="2024-01-02",
            end="2024-01-03",
            benchmark=None,
            cost_model=zero_cost(),
            log_experiment=False,
            trigger_times=["09:31"],  # 日频下应被忽略
        )
        BacktestEngine(strat, cfg, store).run()
        assert [k for k, _ in strat.calls] == ["open", "signal", "close", "open", "signal", "close"]


class TestHookOrders:
    def test_orders_in_hooks_follow_execution_mode(self, tmp_path: Path) -> None:
        """钩子内下单遵守 no-lookahead：next_open 模式次日开盘成交。"""

        class OpenBuyer(Strategy):
            params = {"symbol": SYM}

            def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
                ctx.universe = [str(self.params["symbol"])]

            def on_market_open(self, ctx) -> None:  # type: ignore[no-untyped-def]
                if ctx.position(str(self.params["symbol"])) == 0:
                    ctx.order(str(self.params["symbol"]), 100)

        store = make_market_store(tmp_path, symbols=(SYM,), days=3, base=10.0, benchmark=None)
        cfg = BacktestConfig(
            start="2024-01-02",
            end="2024-01-04",
            benchmark=None,
            cost_model=zero_cost(),
            log_experiment=False,
        )
        result = BacktestEngine(OpenBuyer, cfg, store).run()
        assert len(result.trades) == 1
        trade = result.trades.iloc[0]
        # 第一天 open 钩子下单（看到首日 bar）→ 次日（01-03）开盘成交
        assert trade["date"].date() == pd.Timestamp("2024-01-03").date()
        assert trade["side"] == "buy"
