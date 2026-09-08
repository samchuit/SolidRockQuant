"""回测结果的结构化 JSON 输出（机器读，Agent 消费）.

设计约定：JSON 面向 Agent 与程序；人读看 report.md。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from pathlib import Path

    from solidrock.backtest.engine import BacktestResult


def render_result_json(result: BacktestResult) -> dict:
    """结构化输出（MCP 信封的 data 段可直接引用）。"""

    def _df_records(df: pd.DataFrame) -> list[dict]:
        if df.empty:
            return []
        out = df.copy()
        for col in out.columns:
            if pd.api.types.is_datetime64_any_dtype(out[col]):
                out[col] = out[col].dt.strftime("%Y-%m-%d")
        return out.where(out.notna(), None).to_dict("records")

    nav = result.nav.reset_index().rename(columns={"index": "date"})
    return {
        "run_id": result.run_id,
        "strategy": result.strategy_name,
        "config": result.config.to_dict(),
        "metrics": result.metrics,
        "data_snapshot": result.data_snapshot,
        "nav": _df_records(nav),
        "benchmark": (
            [(d.strftime("%Y-%m-%d"), float(v)) for d, v in result.benchmark.items()]
            if result.benchmark is not None
            else []
        ),
        "trades": _df_records(result.trades),
        "rejections": _df_records(result.rejections),
        "corporate_actions": _df_records(result.corporate_actions),
        "strategy_logs": result.strategy_logs,
    }


def write_result_json(result: BacktestResult, path: Path) -> None:
    path.write_text(
        json.dumps(render_result_json(result), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
