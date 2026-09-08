"""回测引擎端到端测试（合成行情，零费用保证可精确验算）."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from tests.conftest import bars_frame, make_market_store

from solidrock.agent.errors import ErrorCode, SolidRockError
from solidrock.backtest import BacktestConfig, BacktestEngine
from solidrock.backtest.costs import AShareCostModel
from solidrock.data.store import DataStore
from solidrock.strategy.base import Strategy

SYM = "510300.SH"


def zero_cost() -> AShareCostModel:
    return AShareCostModel(
        commission_rate=0.0,
        commission_min=0.0,
        stamp_duty_rate=0.0,
        transfer_fee_rate=0.0,
        slippage_bps=0.0,
    )


class BuyOnce(Strategy):
    params = {"symbol": SYM, "percent": 1.0}

    def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
        ctx.universe = [str(self.params["symbol"])]

    def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
        symbol = str(self.params["symbol"])
        if ctx.position(symbol) == 0:
            ctx.order_target_percent(symbol, float(self.params["percent"]))


class WashTrade(Strategy):
    """同一天买入又卖出（same_close 模式下触发 T+1 拒单）。"""

    def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
        ctx.universe = [SYM]

    def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
        if ctx.position(SYM) == 0:
            ctx.order(SYM, 200)
            ctx.order(SYM, -200)


class FixConfig(Strategy):
    """固定参数买入持有（参数覆盖测试）。"""

    params = {"symbol": SYM}

    def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
        ctx.universe = [str(self.params["symbol"])]

    def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
        if ctx.position(SYM) == 0:
            ctx.order(SYM, 1000)


@pytest.fixture
def mkt(tmp_path: Path):
    return make_market_store(tmp_path, symbols=(SYM,), days=10, base=10.0, drift=0.1)


def config(mkt, **overrides) -> BacktestConfig:
    defaults: dict = {
        "start": "2024-01-02",
        "end": "2024-01-15",
        "benchmark": None,
        "cost_model": zero_cost(),
        "log_experiment": False,
        # 精确验算用；cap 行为在专门测试中开启
        "max_position_weight": None,
    }
    defaults.update(overrides)
    return BacktestConfig(**defaults)


class TestEngineBasics:
    def test_buy_and_hold_exact(self, mkt) -> None:
        """零费用 + 无滑点：day0 收盘(10.0)出信号，day1 开盘(10.1×0.99)成交。"""
        result = BacktestEngine(BuyOnce, config(mkt), mkt).run()
        assert len(result.trades) == 1
        trade = result.trades.iloc[0]
        assert trade["side"] == "buy"
        assert trade["price"] == pytest.approx(10.1 * 0.99)  # day1 开盘
        # 目标市值 1e6 / 参考价 10.0（day0 收盘）= 100000 股（已是整手）
        assert trade["qty"] == pytest.approx(100_000.0)
        last_close = 10.0 + 0.1 * 9  # 10.9
        assert result.nav["total"].iloc[-1] == pytest.approx(100_000.0 * last_close + 100.0)
        assert result.metrics["n_trades"] == 1.0

    def test_no_lookahead_next_open(self, mkt) -> None:
        """成交价必须是执行日开盘价（≠信号日收盘）。"""
        result = BacktestEngine(BuyOnce, config(mkt), mkt).run()
        trade = result.trades.iloc[0]
        assert trade["price"] != pytest.approx(10.0)  # 信号日收盘 10.0，成交在次日开盘 9.999
        assert trade["date"] > result.nav.index[0]

    def test_same_close_fills_at_close(self, mkt) -> None:
        result = BacktestEngine(BuyOnce, config(mkt, execution="same_close"), mkt).run()
        trade = result.trades.iloc[0]
        assert trade["price"] == pytest.approx(10.0)  # day0 收盘
        assert trade["date"] == result.nav.index[0]

    def test_determinism(self, mkt) -> None:
        r1 = BacktestEngine(BuyOnce, config(mkt), mkt).run()
        r2 = BacktestEngine(BuyOnce, config(mkt), mkt).run()
        assert r1.nav.equals(r2.nav)
        assert r1.metrics["sharpe"] == r2.metrics["sharpe"]

    def test_unknown_param_rejected(self, mkt) -> None:
        with pytest.raises(SolidRockError) as exc_info:
            BacktestEngine(lambda: BuyOnce(nonexistent=1), config(mkt), mkt).run()
        assert exc_info.value.code == ErrorCode.PARAM_INVALID

    def test_universe_missing_data(self, mkt) -> None:
        class NoData(Strategy):
            def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
                ctx.universe = ["000001.SZ"]

            def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
                pass

        with pytest.raises(SolidRockError) as exc_info:
            BacktestEngine(NoData, config(mkt), mkt).run()
        assert exc_info.value.code == ErrorCode.NO_DATA
        assert exc_info.value.hint  # 提示先更新数据


class TestRisk:
    def test_weight_cap(self, mkt) -> None:
        result = BacktestEngine(BuyOnce, config(mkt, max_position_weight=0.5), mkt).run()
        trade = result.trades.iloc[0]
        assert trade["qty"] == pytest.approx(50_000.0)  # 总值一半 → 50000 股
        weight = trade["qty"] * trade["price"] / 1_000_000.0
        assert weight <= 0.5 + 1e-9

    def test_drawdown_halt(self, tmp_path: Path) -> None:
        """持续下跌触发熔断：清仓 + 后续策略订单被拒。"""
        mkt = make_market_store(tmp_path, symbols=(SYM,), days=12, base=20.0, drift=-1.0)
        result = BacktestEngine(BuyOnce, config(mkt, drawdown_halt=0.10), mkt).run()
        codes = set(result.rejections["code"])
        assert "HALTED" in codes
        liquidations = result.trades[result.trades["side"] == "sell"]
        assert len(liquidations) >= 1
        assert result.final_positions == {}  # 已清仓


class TestCorporateAction:
    def test_factor_change_adjusts_shares(self, tmp_path: Path) -> None:
        """持有期间 10送10（价格减半、因子翻倍）：价值连续、股数翻倍。"""
        dates = pd.bdate_range("2024-01-02", periods=8)
        closes = [20.0] * 4 + [10.0] * 4
        factors = [1.0] * 4 + [2.0] * 4
        pre_closes = [20.0, 20.0, 20.0, 20.0, 10.0, 10.0, 10.0, 10.0]
        from solidrock.data.store import DataStore

        store = DataStore(tmp_path / "ca")
        store.save_calendar(pd.DataFrame({"date": dates}))
        store.save_bars(bars_frame(SYM, dates, closes, pre_closes=pre_closes, factors=factors))

        result = BacktestEngine(BuyOnce, config(store, start="2024-01-02", end="2024-01-11"), store).run()
        # day0 信号（参考收盘 20.0）→ 50000 股，day1 开盘 19.8 成交
        assert len(result.corporate_actions) == 1
        ca = result.corporate_actions.iloc[0]
        assert ca["ratio"] == pytest.approx(2.0)
        final = result.final_positions[SYM]
        assert final["shares"] == pytest.approx(100_000.0)  # 50000 × 2
        assert final["avg_cost"] == pytest.approx(9.9)  # 19.8 / 2
        # 价值连续：除权日 nav 与前一日完全一致（50000×20 == 100000×10）
        nav = result.nav["total"]
        boundary_prev = nav.iloc[3]
        boundary = nav.iloc[4]
        assert boundary == pytest.approx(boundary_prev)


class TestSuspension:
    def test_order_expires_when_suspended(self, tmp_path: Path) -> None:
        dates = pd.bdate_range("2024-01-02", periods=6)
        store = DataStore(tmp_path / "susp")
        store.save_calendar(pd.DataFrame({"date": dates}))
        frame = bars_frame(SYM, dates, [10.0] * 6)
        # 去掉第 3 个交易日（01-04 停牌）
        store.save_bars(frame.drop(frame.index[2]), source="synthetic")

        class BuyOnSecondDay(Strategy):
            """01-03 出信号 → 订单落在 01-04（停牌日）→ 应被记为 SUSPENDED。"""

            def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
                ctx.universe = [SYM]

            def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
                if ctx.now == pd.Timestamp("2024-01-03"):
                    ctx.order_target_percent(SYM, 1.0)

        result = BacktestEngine(BuyOnSecondDay, config(store, start="2024-01-02", end="2024-01-09"), store).run()
        assert "SUSPENDED" in set(result.rejections["code"])


class TestBenchmarkAndArtifacts:
    def test_benchmark_and_metrics(self, tmp_path: Path) -> None:
        mkt = make_market_store(tmp_path, symbols=(SYM,), days=10, base=10.0, drift=0.1, benchmark="000300.SH")
        cfg = config(mkt, benchmark="000300.SH")
        result = BacktestEngine(BuyOnce, cfg, mkt).run()
        assert result.benchmark is not None
        assert result.metrics["benchmark_total_return"] == pytest.approx(0.1 * 9 / 15.0, rel=0.01)
        assert "excess_total_return" in result.metrics

    def test_experiment_artifacts_written(self, tmp_path: Path) -> None:
        mkt = make_market_store(tmp_path, symbols=(SYM,), days=10, base=10.0, drift=0.1)
        cfg = config(mkt, benchmark=None, log_experiment=True, name="测试实验")
        result = BacktestEngine(BuyOnce, cfg, mkt).run()
        assert result.artifacts_dir is not None
        for name in ("report.md", "result.json", "trades.csv", "nav.csv"):
            assert (result.artifacts_dir / name).exists(), name

        import json

        payload = json.loads((result.artifacts_dir / "result.json").read_text(encoding="utf-8"))
        assert payload["strategy"] == "BuyOnce"
        assert "sharpe" in payload["metrics"]

        from solidrock.experiments.tracker import ExperimentTracker

        tracker = ExperimentTracker(mkt.root / "experiments.db")
        runs = tracker.list_runs(kind="backtest")
        assert len(runs) == 1
        assert runs[0]["name"] == "测试实验"
        assert result.metrics["sharpe"] == runs[0]["metrics"]["sharpe"]

    def test_report_markdown_content(self, tmp_path: Path) -> None:
        mkt = make_market_store(tmp_path, symbols=(SYM,), days=10, base=10.0, drift=0.1)
        result = BacktestEngine(BuyOnce, config(mkt, execution="same_close"), mkt).run()
        from solidrock.report.markdown import render_report_markdown

        text = render_report_markdown(result)
        assert "BuyOnce" in text
        assert "same_close" in text  # 前视风险标注
        assert "核心指标" in text

    def test_data_snapshot_recorded(self, tmp_path: Path) -> None:
        mkt = make_market_store(tmp_path, symbols=(SYM,), days=10, base=10.0)
        snap = mkt.create_snapshot("snap-bt")
        cfg = config(snap, log_experiment=False)
        result = BacktestEngine(BuyOnce, cfg, snap).run()
        assert result.data_snapshot == "snap-bt"


class TestHistory:
    def test_history_includes_current_bar(self, mkt) -> None:
        seen: dict = {}

        class Probe(Strategy):
            def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
                ctx.universe = [SYM]

            def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
                if ctx.now == pd.Timestamp("2024-01-04"):
                    seen["close"] = ctx.history(SYM, 3, fields="close")[SYM].tolist()
                    seen["now"] = ctx.now

        BacktestEngine(Probe, config(mkt), mkt).run()
        assert seen["now"] == pd.Timestamp("2024-01-04")
        # 含当日 bar，且能取到 warmup 之前的数据
        assert len(seen["close"]) == 3
        assert not np.isnan(seen["close"]).any()
