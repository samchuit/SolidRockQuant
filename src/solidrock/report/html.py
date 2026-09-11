"""回测报告输出：自包含 HTML（plotly 交互图，人读）.

依赖 plotly（extras ``report``）。产物为单文件 HTML，plotly.js 内联，
离线双击即可打开。JSON 结构化输出见 json_report.py（Agent 消费）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from solidrock.agent.errors import ErrorCode, err
from solidrock.report.metrics import format_metrics, monthly_returns

if TYPE_CHECKING:
    from pathlib import Path

    from solidrock.backtest.engine import BacktestResult

_ROLLING_WINDOW = 60


def render_report_html(result: BacktestResult) -> str:
    """渲染单文件 HTML 交互报告：资金/回撤、月度热力图、收益分布、滚动夏普."""
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError as e:
        raise err(
            ErrorCode.SOURCE_UNAVAILABLE,
            "HTML 报告需要 plotly",
            hint="pip install 'solidrock-quant[report]'",
        ) from e

    nav = result.nav["total"].dropna()
    cfg = result.config
    figures: list[tuple[str, str]] = []
    first = True

    def _fig_html(fig: go.Figure) -> str:
        nonlocal first
        html = fig.to_html(
            full_html=False, include_plotlyjs="inline" if first else False, config={"displaylogo": False}
        )
        first = False
        return html

    # 1. 资金曲线与回撤（净值归一，便于与基准对比）
    if len(nav) >= 2:
        returns = nav.pct_change().dropna()
        norm = nav / nav.iloc[0]
        fig = make_subplots(
            rows=2,
            cols=1,
            shared_xaxes=True,
            row_heights=[0.7, 0.3],
            vertical_spacing=0.06,
            subplot_titles=["净值 vs 基准（期初=1）", "回撤"],
        )
        fig.add_trace(go.Scatter(x=norm.index, y=norm, name="策略净值", line={"color": "#1f77b4"}), row=1, col=1)
        if result.benchmark is not None and len(result.benchmark) >= 2:
            bench = result.benchmark.reindex(norm.index).dropna()
            bench = bench / bench.iloc[0]
            fig.add_trace(
                go.Scatter(x=bench.index, y=bench, name="基准", line={"color": "#ff7f0e", "dash": "dash"}), row=1, col=1
            )
        dd = nav / nav.cummax() - 1.0
        fig.add_trace(
            go.Scatter(x=dd.index, y=dd, name="回撤", fill="tozeroy", line={"color": "#d62728"}), row=2, col=1
        )
        fig.update_layout(height=520, legend={"orientation": "h"}, margin={"t": 48, "b": 24})
        figures.append(("资金曲线与回撤", _fig_html(fig)))

        # 2. 月度收益热力图
        monthly = monthly_returns(nav)
        if len(monthly) >= 2:
            frame = pd.DataFrame({"v": monthly.values}, index=pd.PeriodIndex(monthly.index, freq="M"))
            pivot = frame["v"].groupby([frame.index.year, frame.index.month]).sum().unstack()
            fig_m = go.Figure(
                go.Heatmap(
                    z=pivot.values,
                    x=[f"{m}月" for m in pivot.columns],
                    y=[str(y) for y in pivot.index],
                    colorscale="RdYlGn",
                    zmid=0.0,
                    text=[[("" if pd.isna(v) else f"{v:.1%}") for v in row] for row in pivot.values],
                    texttemplate="%{text}",
                    hovertemplate="%{y}-%{x}: %{z:.2%}<extra></extra>",
                )
            )
            fig_m.update_layout(height=300, margin={"t": 24, "b": 24})
            figures.append(("月度收益热力图", _fig_html(fig_m)))

        # 3. 日收益分布
        fig_d = go.Figure(go.Histogram(x=returns, nbinsx=60, marker={"color": "#1f77b4"}))
        fig_d.update_layout(height=320, margin={"t": 24, "b": 24})
        figures.append(("日收益分布", _fig_html(fig_d)))

        # 4. 滚动夏普（窗口不足时省略）
        if len(returns) > _ROLLING_WINDOW * 2:
            roll = returns.rolling(_ROLLING_WINDOW).mean() / returns.rolling(_ROLLING_WINDOW).std(ddof=1)
            roll = roll * np.sqrt(252)
            fig_r = go.Figure(
                go.Scatter(x=roll.index, y=roll, name=f"滚动夏普({_ROLLING_WINDOW}日)", line={"color": "#2ca02c"})
            )
            fig_r.add_hline(y=0, line_dash="dot", line_color="#999")
            fig_r.update_layout(height=320, margin={"t": 24, "b": 24})
            figures.append((f"滚动夏普（{_ROLLING_WINDOW} 日）", _fig_html(fig_r)))

    metric_rows = "".join(
        f"<tr><td>{label}</td><td class='num'>{value}</td></tr>" for label, value in format_metrics(result.metrics)
    )
    reject_html = ""
    if len(result.rejections) > 0:
        rows = "".join(
            f"<tr><td>{row['date'].date()}</td><td>{row['symbol']}</td><td>{row['side']}</td>"
            f"<td class='num'>{row['qty']:.0f}</td><td>{row['code']}</td></tr>"
            for _, row in result.rejections.head(20).iterrows()
        )
        note = f"（共 {len(result.rejections)} 条，仅显示前 20）" if len(result.rejections) > 20 else ""
        reject_html = (
            f"<h2>拒单 {note}</h2><table><tr><th>日期</th><th>符号</th><th>方向</th>"
            f"<th>数量</th><th>原因</th></tr>{rows}</table>"
        )

    sections = "".join(f"<h2>{title}</h2>{html}" for title, html in figures)
    warning = (
        "<p class='warn'>⚠️ same_close 执行模式（信号当日收盘成交）存在前视风险，仅用于快速研究。</p>"
        if cfg.execution == "same_close"
        else ""
    )
    return (
        "<!DOCTYPE html>\n<html lang='zh-CN'>\n<head>\n<meta charset='utf-8'>\n"
        f"<title>回测报告 · {result.strategy_name}</title>\n"
        "<style>"
        "body{font-family:'Segoe UI','Microsoft YaHei',sans-serif;max-width:1080px;margin:24px auto;"
        "padding:0 16px;color:#222}"
        "h1{border-bottom:2px solid #1f77b4;padding-bottom:8px}"
        "h2{margin-top:36px;color:#1f77b4}"
        "table{border-collapse:collapse;margin:8px 0}"
        "th,td{border:1px solid #ccc;padding:4px 12px;text-align:left}"
        "td.num,th.num{text-align:right}"
        "th{background:#f0f4f8}"
        ".meta{color:#666}"
        ".warn{background:#fff3cd;border:1px solid #ffe08a;padding:8px 12px;border-radius:4px}"
        "</style>\n</head>\n<body>\n"
        f"<h1>回测报告 · {result.strategy_name}</h1>\n"
        f"<p class='meta'>Run ID: <code>{result.run_id}</code> ｜ 区间: {cfg.start} ~ {cfg.end}"
        f" ｜ 执行模式: <code>{cfg.execution}</code> ｜ 初始资金: {cfg.initial_cash:,.0f} 元"
        f" ｜ 基准: {cfg.benchmark or '（无）'}"
        + (f" ｜ 数据快照: <code>{result.data_snapshot}</code>" if result.data_snapshot else "")
        + "</p>\n"
        + warning
        + f"<h2>核心指标</h2><table><tr><th>指标</th><th class='num'>数值</th></tr>{metric_rows}</table>\n"
        + sections
        + reject_html
        + "\n</body>\n</html>\n"
    )


def write_report_html(result: BacktestResult, path: Path) -> None:
    path.write_text(render_report_html(result), encoding="utf-8")
