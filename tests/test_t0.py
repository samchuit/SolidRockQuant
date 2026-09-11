"""P2.6 测试：T+0 品种识别与撮合（当日买入当日可卖）."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from solidrock.backtest import BacktestConfig, BacktestEngine
from solidrock.backtest.costs import AShareCostModel
from solidrock.data.store import DataStore
from solidrock.data.symbols import parse_symbol
from solidrock.strategy.base import Strategy
from tests.conftest import bars_frame


def zero_cost() -> AShareCostModel:
    return AShareCostModel(
        commission_rate=0.0,
        commission_min=0.0,
        stamp_duty_rate=0.0,
        transfer_fee_rate=0.0,
        slippage_bps=0.0,
    )


class TestT0Classification:
    @pytest.mark.parametrize(
        ("symbol", "expected"),
        [
            ("511990.SH", True),  # 货币 ETF
            ("513100.SH", True),  # 跨境 ETF
            ("518880.SH", True),  # 黄金 ETF
            ("159941.SZ", True),  # 纳指 ETF（跨境）
            ("159934.SZ", True),  # 黄金 ETF
            ("510300.SH", False),  # 沪市宽基 ETF
            ("159915.SZ", False),  # 深市创业板 ETF
            ("600519.SH", False),  # 股票
            ("RB2505.SHFE", True),  # 期货天然 T+0
        ],
    )
    def test_is_t0(self, symbol: str, expected: bool) -> None:
        assert parse_symbol(symbol).is_t0 is expected


def minute_store(tmp_path: Path, symbol: str = "510300.SH") -> DataStore:
    """单标的合成 5 分钟 bar：2 天 × 4 根（10:00 起），价格线性上行。"""
    store = DataStore(tmp_path / "m5")
    frames = []
    for d in range(2):
        day0 = pd.Timestamp("2024-01-02") + pd.Timedelta(days=d)
        ticks = pd.date_range(day0 + pd.Timedelta(hours=10), periods=4, freq="5min")
        closes = [10.0 + 0.1 * i for i in range(4)]
        frames.append(
            bars_frame(
                symbol,
                ticks,
                closes,
                opens=[c - 0.05 for c in closes],
                highs=[c + 0.05 for c in closes],
                lows=[c - 0.08 for c in closes],
                pre_closes=[10.0 - 0.05, *closes[:-1]],
            )
        )
    store.save_calendar(pd.DataFrame({"date": pd.date_range("2024-01-02", periods=2).normalize()}))
    store.save_bars(pd.concat(frames, ignore_index=True), freq="5m", source="synthetic")
    return store


class BuyThenSellSameDay(Strategy):
    """bar1（10:00）买入，bar3（10:10，同日）卖出——bar4 成交或被拒。"""

    params = {"symbol": "510300.SH"}

    def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
        ctx.universe = [str(self.params["symbol"])]

    def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
        symbol = str(self.params["symbol"])
        if ctx.now.date() != pd.Timestamp("2024-01-02").date():
            return
        if ctx.now == pd.Timestamp("2024-01-02 10:00:00"):
            ctx.order(symbol, 100)
        elif ctx.now == pd.Timestamp("2024-01-02 10:10:00") and ctx.position(symbol) > 0:
            ctx.order(symbol, -100)


def run_minute(tmp_path: Path, symbol: str) -> Any:
    cfg = BacktestConfig(
        start="2024-01-02",
        end="2024-01-03",
        benchmark=None,
        cost_model=zero_cost(),
        log_experiment=False,
        max_position_weight=None,
        freq="5m",
        warmup_bars=16,
    )
    return BacktestEngine(BuyThenSellSameDay(symbol=symbol), cfg, minute_store(tmp_path, symbol)).run()


class TestT0Matching:
    def test_t0_etf_same_day_sell_allowed(self, tmp_path: Path) -> None:
        """T0 品种：当日 bar2 买入，同日 bar4 卖出成交。"""
        result = run_minute(tmp_path, "513100.SH")
        sells = result.trades[result.trades["side"] == "sell"]
        assert len(sells) == 1
        assert sells.iloc[0]["date"].date() == pd.Timestamp("2024-01-02").date()

    def test_t1_etf_same_day_sell_rejected(self, tmp_path: Path) -> None:
        """T+1 品种：同日卖出被拒（T_PLUS_ONE）。"""
        result = run_minute(tmp_path, "510300.SH")
        sells = result.trades[result.trades["side"] == "sell"]
        assert len(sells) == 0
        assert (result.rejections["code"] == "T_PLUS_ONE").any()

    def test_t0_buy_unlocks_next_day_too(self, tmp_path: Path) -> None:
        """T0 品种换日 release_available 后仍可正常卖出。"""

        class BuyDay1SellDay2(Strategy):
            params = {"symbol": "513100.SH"}

            def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
                ctx.universe = [str(self.params["symbol"])]

            def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
                symbol = str(self.params["symbol"])
                if ctx.now.date() == pd.Timestamp("2024-01-02").date():
                    if ctx.position(symbol) == 0:
                        ctx.order(symbol, 100)
                elif ctx.position(symbol) > 0:
                    ctx.order(symbol, -100)

        cfg = BacktestConfig(
            start="2024-01-02",
            end="2024-01-03",
            benchmark=None,
            cost_model=zero_cost(),
            log_experiment=False,
            max_position_weight=None,
            freq="5m",
            warmup_bars=16,
        )
        result = BacktestEngine(BuyDay1SellDay2(), cfg, minute_store(tmp_path, "513100.SH")).run()
        sells = result.trades[result.trades["side"] == "sell"]
        assert len(sells) == 1
        assert sells.iloc[0]["date"].date() == pd.Timestamp("2024-01-03").date()
