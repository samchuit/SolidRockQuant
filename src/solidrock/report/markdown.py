"""回测报告输出：Markdown（人读）.

JSON 结构化输出见 json_report.py（Agent 消费）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from solidrock.report.metrics import format_metrics, yearly_returns

if TYPE_CHECKING:
    from pathlib import Path

    from solidrock.backtest.engine import BacktestResult


def render_report_markdown(result: BacktestResult) -> str:
    """把回测结果渲染为 Markdown 报告."""
    cfg = result.config
    lines: list[str] = []
    lines.append(f"# 回测报告 · {result.strategy_name}")
    lines.append("")
    lines.append(f"- **Run ID**: `{result.run_id}`")
    lines.append(f"- **区间**: {cfg.start} ~ {cfg.end}（执行模式：`{cfg.execution}`）")
    lines.append(f"- **初始资金**: {cfg.initial_cash:,.0f} 元")
    lines.append(f"- **基准**: {cfg.benchmark or '（无）'}")
    if result.data_snapshot:
        lines.append(f"- **数据快照**: `{result.data_snapshot}`")
    lines.append("")

    lines.append("## 核心指标")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    for label, value in format_metrics(result.metrics):
        lines.append(f"| {label} | {value} |")
    lines.append("")

    yearly = yearly_returns(result.nav["total"])
    if len(yearly) > 0:
        lines.append("## 分年度收益")
        lines.append("")
        lines.append("| 年份 | 收益 |")
        lines.append("|------|------|")
        for year, ret in yearly.items():
            lines.append(f"| {year} | {ret * 100:.2f}% |")
        lines.append("")

    if len(result.trades) > 0:
        lines.append("## 交易摘要")
        lines.append("")
        buys = result.trades[result.trades["side"] == "buy"]
        sells = result.trades[result.trades["side"] == "sell"]
        lines.append(f"- 成交 {len(result.trades)} 笔（买 {len(buys)} / 卖 {len(sells)}）")
        if len(sells) > 0 and sells["pnl"].notna().any():
            best = sells["pnl"].max()
            worst = sells["pnl"].min()
            lines.append(f"- 单笔已实现盈亏区间：{worst:,.2f} ~ {best:,.2f} 元")
        lines.append("")

    if len(result.rejections) > 0:
        lines.append("## 拒单（前 10 条）")
        lines.append("")
        lines.append("| 日期 | 符号 | 方向 | 数量 | 原因 |")
        lines.append("|------|------|------|------|------|")
        for _, row in result.rejections.head(10).iterrows():
            lines.append(
                f"| {row['date'].date()} | {row['symbol']} | {row['side']} | {row['qty']:.0f} | {row['code']} |"
            )
        if len(result.rejections) > 10:
            lines.append(f"\n（其余 {len(result.rejections) - 10} 条见 result.json / 拒单记录）")
        lines.append("")

    if len(result.corporate_actions) > 0:
        lines.append("## 公司行为（复权因子变化触发的持仓调整）")
        lines.append("")
        for _, row in result.corporate_actions.iterrows():
            lines.append(
                f"- {row['date'].date()} {row['symbol']}：因子比例 {row['ratio']:.4f}，"
                f"持股 {row['shares_before']:.0f} 股相应调整"
            )
        lines.append("")

    if result.strategy_logs:
        lines.append("## 策略日志（前 20 条）")
        lines.append("")
        for entry in result.strategy_logs[:20]:
            lines.append(f"- {entry}")
        lines.append("")

    if cfg.execution == "same_close":
        lines.append("> ⚠️ 本回测使用 `same_close` 执行模式（信号当日收盘成交），存在前视风险，仅用于快速研究。")
        lines.append("")
    return "\n".join(lines)


def write_report_markdown(result: BacktestResult, path: Path) -> None:
    path.write_text(render_report_markdown(result), encoding="utf-8")
