"""因子分析报告：Markdown（人读）与 JSON（机器读）."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from solidrock.factors.analysis import FactorAnalysisResult


def _pct(value: float) -> str:
    return "nan" if value != value else f"{value * 100:.2f}%"


def render_factor_report(result: FactorAnalysisResult) -> str:
    lines: list[str] = []
    lines.append(f"# 因子分析报告 · {result.factor_name}")
    lines.append("")
    lines.append(f"- **Run ID**: `{result.run_id}`")
    lines.append(
        f"- **区间**: {result.config['start']} ~ {result.config['end']}"
        f"（前瞻 {result.config['fwd_period']} 日，{result.config['quantiles']} 层）"
    )
    lines.append(f"- **样本**: {len(result.config['universe'])} 个标的")
    if result.data_snapshot:
        lines.append(f"- **数据快照**: `{result.data_snapshot}`")
    lines.append("")
    if result.params:
        lines.append(f"- **参数**: {result.params}")
        lines.append("")

    ic = result.ic_summary
    lines.append("## RankIC 摘要")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    lines.append(f"| IC 均值 | {ic['ic_mean']:.4f} |")
    lines.append(f"| IC 标准差 | {ic['ic_std']:.4f} |")
    lines.append(f"| ICIR | {ic['ic_ir']:.3f} |")
    lines.append(f"| IC t 值 | {ic['ic_t_stat']:.2f} |")
    lines.append(f"| 有效截面数 | {ic['n_days']} |")
    lines.append(f"| IC > 0 占比 | {ic['positive_ratio'] * 100:.1f}% |")
    lines.append(f"| 因子自相关 | {result.factor_autocorr:.3f} |")
    lines.append("")

    lines.append("## 分层表现（逐日再平衡、等权）")
    lines.append("")
    lines.append("| 层 | 累计收益 | 年化收益 | 夏普 |")
    lines.append("|----|----------|----------|------|")
    for layer, row in result.layer_stats.iterrows():
        label = "多空 L-S" if layer == "L-S" else f"第 {layer} 层"
        lines.append(f"| {label} | {_pct(row['total_return'])} | {_pct(row['annual_return'])} | {row['sharpe']:.2f} |")
    lines.append("")
    lines.append("> 层 1 为因子值最低组，层 q 为最高组；单层收益未计交易成本。")
    lines.append("")
    return "\n".join(lines)


def render_factor_json(result: FactorAnalysisResult) -> dict:
    ic_records = [{"date": d.strftime("%Y-%m-%d"), "rank_ic": float(v)} for d, v in result.ic.items()]
    return {
        "run_id": result.run_id,
        "factor": result.factor_name,
        "params": result.params,
        "config": result.config,
        "ic_summary": result.ic_summary,
        "factor_autocorr": result.factor_autocorr,
        "ic": ic_records,
        "layer_stats": result.layer_stats.to_dict(orient="index"),
        "layer_navs": {
            str(col): [(d.strftime("%Y-%m-%d"), float(v)) for d, v in result.layer_navs[col].items()]
            for col in result.layer_navs.columns
        },
        "data_snapshot": result.data_snapshot,
    }


def write_factor_json(result: FactorAnalysisResult, path) -> None:
    path.write_text(
        json.dumps(render_factor_json(result), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
