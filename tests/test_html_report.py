"""P1.1 测试：自包含 HTML 交互回测报告."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("plotly", reason="HTML 报告依赖 plotly（extras report）")

from solidrock.backtest.config import BacktestConfig
from solidrock.backtest.engine import BacktestResult
from solidrock.report.html import render_report_html, write_report_html
from solidrock.report.metrics import compute_metrics


def make_result() -> BacktestResult:
    dates = pd.bdate_range("2024-01-02", periods=180)
    close = 10.0 + np.linspace(0.0, 3.0, 180) + 0.2 * np.sin(np.arange(180) / 5.0)
    nav = pd.DataFrame(
        {"total": close * 100_000.0, "cash": 50_000.0, "market_value": close * 100_000.0 - 50_000.0},
        index=dates,
    )
    bench = pd.Series(np.linspace(1.0, 1.1, 180), index=dates, name="benchmark")
    trades = pd.DataFrame(
        {
            "date": dates[:2],
            "symbol": ["510300.SH", "510300.SH"],
            "side": ["buy", "sell"],
            "qty": [1000.0, 1000.0],
            "price": [10.0, 11.0],
            "value": [10_000.0, 11_000.0],
            "fees": [5.0, 5.5],
            "pnl": [np.nan, 995.0],
            "closing": [False, True],
        }
    )
    metrics = compute_metrics(nav["total"], bench, trades)
    return BacktestResult(
        run_id="bt-test-0001",
        strategy_name="TestStrategy",
        config=BacktestConfig(start=dates[0], end=dates[-1], name="test"),
        nav=nav,
        trades=trades,
        rejections=pd.DataFrame(
            {
                "date": [dates[3]],
                "symbol": ["510300.SH"],
                "side": ["buy"],
                "qty": [100.0],
                "code": ["LIMIT_UP"],
            }
        ),
        corporate_actions=pd.DataFrame(),
        metrics=metrics,
        benchmark=bench,
        strategy_logs=["2024-01-02 买入 510300.SH"],
        data_snapshot=None,
        artifacts_dir=None,
    )


class TestHtmlReport:
    def test_renders_self_contained_html(self) -> None:
        html = render_report_html(make_result())
        assert html.startswith("<!DOCTYPE html>")
        assert "回测报告 · TestStrategy" in html
        # plotly.js 内联（自包含、离线可开）
        assert "Plotly.newPlot" in html
        assert len(html) > 1_000_000  # 内联 plotly.js 的体积特征
        # 图表与指标齐全
        for section in ("资金曲线与回撤", "月度收益热力图", "日收益分布", "滚动夏普"):
            assert section in html
        assert "索提诺比率" in html
        assert "阿尔法" in html
        assert "LIMIT_UP" in html  # 拒单表
        assert "same_close" not in html or "next_open" in html  # 执行模式标注

    def test_write_to_file(self, tmp_path: Path) -> None:
        path = tmp_path / "report.html"
        write_report_html(make_result(), path)
        assert path.exists() and path.stat().st_size > 1_000_000

    def test_minimal_nav(self) -> None:
        result = make_result()
        result.benchmark = None
        result.nav = result.nav.iloc[:1]  # 不足 2 行：优雅降级为纯指标表
        html = render_report_html(result)
        assert "核心指标" in html
        assert "Plotly.newPlot" not in html
