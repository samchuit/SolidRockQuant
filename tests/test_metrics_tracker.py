"""M4 测试：绩效指标、实验追踪."""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from solidrock.agent.errors import ErrorCode, SolidRockError
from solidrock.experiments.tracker import ExperimentTracker
from solidrock.report.metrics import compute_metrics, format_metrics, yearly_returns


def nav_series(values: list[float], start: str = "2024-01-02") -> pd.Series:
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)), name="total")


class TestMetrics:
    def test_basic_metrics(self) -> None:
        nav = nav_series([100.0, 110.0, 99.0, 121.0])
        m = compute_metrics(nav)
        assert m["total_return"] == pytest.approx(0.21)
        assert m["max_drawdown"] == pytest.approx(-0.1)  # 99/110 - 1
        years = 3 / 252
        assert m["annual_return"] == pytest.approx(1.21 ** (1 / years) - 1, rel=1e-6)
        returns = nav.pct_change().dropna()
        assert m["annual_vol"] == pytest.approx(returns.std(ddof=1) * math.sqrt(252), rel=1e-6)
        assert m["sharpe"] == pytest.approx(returns.mean() / returns.std(ddof=1) * math.sqrt(252), rel=1e-6)
        assert m["calmar"] == pytest.approx(m["annual_return"] / 0.1, rel=1e-6)
        assert m["daily_win_rate"] == pytest.approx(2 / 3)

    def test_benchmark_and_trades(self) -> None:
        nav = nav_series([100.0, 110.0, 99.0, 121.0])
        bench = nav_series([100.0, 102.0, 101.0, 105.0], start="2024-01-02")
        trades = pd.DataFrame(
            {
                "side": ["buy", "sell", "sell", "sell"],
                "value": [1000.0, 1100.0, 900.0, 1000.0],
                "fees": [5.0, 5.0, 5.0, 5.0],
                "pnl": [None, 100.0, -50.0, 20.0],
            }
        )
        m = compute_metrics(nav, bench, trades)
        assert m["benchmark_total_return"] == pytest.approx(0.05)
        assert m["excess_total_return"] == pytest.approx(0.21 - 0.05)
        assert m["trade_win_rate"] == pytest.approx(2 / 3)
        assert m["profit_factor"] == pytest.approx(120.0 / 50.0)
        assert m["total_fees"] == 20.0
        assert m["annual_turnover"] > 0

    def test_degenerate_nav(self) -> None:
        m = compute_metrics(pd.Series([100.0]))
        assert m["total_return"] == 0.0
        assert m["sharpe"] == 0.0

    def test_yearly_returns(self) -> None:
        idx = pd.bdate_range("2024-01-01", periods=4).tolist() + pd.bdate_range("2025-01-01", periods=2).tolist()
        nav = pd.Series([100.0, 110.0, 121.0, 100.0, 110.0, 121.0], index=idx)
        yearly = yearly_returns(nav)
        assert list(yearly.index) == [2024, 2025]
        assert yearly[2024] == pytest.approx(1.21 * (100 / 121) - 1)

    def test_format_metrics(self) -> None:
        rows = dict(format_metrics({"sharpe": 1.234, "total_return": 0.215, "n_trades": 12.0}))
        assert rows["夏普比率"] == "1.23"
        assert rows["累计收益"] == "21.50%"
        assert rows["成交笔数"] == "12"


class TestTracker:
    def test_roundtrip(self, tmp_path: Path) -> None:
        tracker = ExperimentTracker(tmp_path / "exp" / "experiments.db")
        id1 = tracker.log_run(
            kind="backtest",
            name="DualMA",
            config={"start": "2024-01-01", "fast": 5},
            metrics={"sharpe": 1.2, "annual_return": 0.15, "max_drawdown": -0.08},
            data_snapshot="snap-x",
        )
        id2 = tracker.log_run(
            kind="backtest",
            name="DualMA",
            config={"start": "2024-01-01", "fast": 10},
            metrics={"sharpe": 0.9, "annual_return": 0.10, "max_drawdown": -0.12},
        )
        runs = tracker.list_runs(kind="backtest")
        assert len(runs) == 2
        assert {r["id"] for r in runs} == {id1, id2}

        run = tracker.get_run(id1)
        assert run["config"]["fast"] == 5
        assert run["metrics"]["sharpe"] == 1.2
        assert run["data_snapshot"] == "snap-x"

        df = tracker.compare([id1, id2])
        assert df.loc["sharpe", id1] == 1.2
        assert df.loc["sharpe", id2] == 0.9

    def test_kind_filter(self, tmp_path: Path) -> None:
        tracker = ExperimentTracker(tmp_path / "exp.db")
        tracker.log_run(kind="backtest", name="A", config={}, metrics={})
        tracker.log_run(kind="factor", name="F", config={}, metrics={})
        assert len(tracker.list_runs(kind="factor")) == 1
        assert len(tracker.list_runs()) == 2

    def test_get_missing_run(self, tmp_path: Path) -> None:
        tracker = ExperimentTracker(tmp_path / "exp.db")
        with pytest.raises(SolidRockError) as exc_info:
            tracker.get_run("nope")
        assert exc_info.value.code == ErrorCode.NO_DATA

    def test_compare_needs_two(self, tmp_path: Path) -> None:
        tracker = ExperimentTracker(tmp_path / "exp.db")
        run_id = tracker.log_run(kind="backtest", name="A", config={}, metrics={})
        with pytest.raises(SolidRockError) as exc_info:
            tracker.compare([run_id])
        assert exc_info.value.code == ErrorCode.PARAM_INVALID

    def test_metrics_json_survives_nan_free(self, tmp_path: Path) -> None:
        tracker = ExperimentTracker(tmp_path / "exp.db")
        run_id = tracker.log_run(
            kind="backtest",
            name="A",
            config={},
            metrics={"sharpe": float("nan")},  # NaN 应能存取（json default=str）
        )
        run = tracker.get_run(run_id)
        assert math.isnan(float(run["metrics"]["sharpe"]))
