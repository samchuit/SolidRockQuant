"""绩效指标计算.

所有年化按 252 个交易日；无风险利率 v0.1 取 0。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Mapping

_TRADING_DAYS_PER_YEAR = 252


def _safe_float(value: object) -> float:
    f = float(value)  # type: ignore[arg-type]
    return f if not math.isnan(f) else 0.0


def compute_metrics(
    nav: pd.Series,
    benchmark: pd.Series | None = None,
    trades: pd.DataFrame | None = None,
) -> dict[str, float]:
    """从净值曲线与交易明细计算核心指标；数据不足时各项优雅降级为 0/None."""
    out: dict[str, float] = {}
    nav = nav.dropna()
    n = len(nav)
    if n < 2:
        return {
            "total_return": 0.0,
            "annual_return": 0.0,
            "annual_vol": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "calmar": 0.0,
            "daily_win_rate": 0.0,
        }
    returns = nav.pct_change().dropna()
    total_return = _safe_float(nav.iloc[-1] / nav.iloc[0] - 1.0)
    years = (n - 1) / _TRADING_DAYS_PER_YEAR
    annual_return = _safe_float((nav.iloc[-1] / nav.iloc[0]) ** (1.0 / years) - 1.0) if years > 0 else 0.0
    std = _safe_float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    annual_vol = std * math.sqrt(_TRADING_DAYS_PER_YEAR)
    sharpe = _safe_float(returns.mean() / std * math.sqrt(_TRADING_DAYS_PER_YEAR)) if std > 1e-12 else 0.0
    drawdown = nav / nav.cummax() - 1.0
    max_drawdown = _safe_float(drawdown.min())
    calmar = _safe_float(annual_return / abs(max_drawdown)) if abs(max_drawdown) > 1e-12 else 0.0

    out.update(
        total_return=total_return,
        annual_return=annual_return,
        annual_vol=annual_vol,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        calmar=calmar,
        daily_win_rate=_safe_float((returns > 0).mean()),
    )

    # 交易层面
    if trades is not None and len(trades) > 0:
        out["n_trades"] = float(len(trades))
        out["total_fees"] = _safe_float(trades["fees"].sum())
        sells = trades[(trades["side"] == "sell") & trades["pnl"].notna()]
        if len(sells) > 0:
            out["trade_win_rate"] = _safe_float((sells["pnl"] > 0).mean())
            gains = _safe_float(sells.loc[sells["pnl"] > 0, "pnl"].sum())
            losses = _safe_float(-sells.loc[sells["pnl"] < 0, "pnl"].sum())
            out["profit_factor"] = _safe_float(gains / losses) if losses > 1e-12 else float("inf") if gains > 0 else 0.0
        # 年化换手率：双边成交额 / 2 / 平均净值 / 年
        if n > 1:
            years_bt = (n - 1) / _TRADING_DAYS_PER_YEAR
            avg_nav = _safe_float(nav.mean())
            if avg_nav > 0 and years_bt > 0:
                out["annual_turnover"] = _safe_float(trades["value"].sum() / 2.0 / avg_nav / years_bt)

    # 基准对比
    if benchmark is not None and len(benchmark) >= 2:
        b_total = _safe_float(benchmark.iloc[-1] / benchmark.iloc[0] - 1.0)
        b_years = (len(benchmark) - 1) / _TRADING_DAYS_PER_YEAR
        b_annual = (
            _safe_float((benchmark.iloc[-1] / benchmark.iloc[0]) ** (1.0 / b_years) - 1.0) if b_years > 0 else 0.0
        )
        out["benchmark_total_return"] = b_total
        out["benchmark_annual_return"] = b_annual
        out["excess_total_return"] = total_return - b_total
        out["excess_annual_return"] = annual_return - b_annual
    return out


def yearly_returns(nav: pd.Series) -> pd.Series:
    """分年度收益（报告用）。"""
    if len(nav) < 2:
        return pd.Series(dtype="float64")
    daily = nav.pct_change()
    grouped = (1 + daily.fillna(0)).groupby(nav.index.year).prod() - 1
    return grouped


def format_metrics(metrics: Mapping[str, float]) -> list[tuple[str, str]]:
    """指标 → (名称, 格式化值) 列表（报告/CLI 共用）。"""
    as_pct = {
        "total_return",
        "annual_return",
        "annual_vol",
        "max_drawdown",
        "daily_win_rate",
        "trade_win_rate",
        "benchmark_total_return",
        "benchmark_annual_return",
        "excess_total_return",
        "excess_annual_return",
        "annual_turnover",
    }
    names = {
        "total_return": "累计收益",
        "annual_return": "年化收益",
        "annual_vol": "年化波动",
        "sharpe": "夏普比率",
        "max_drawdown": "最大回撤",
        "calmar": "卡玛比率",
        "daily_win_rate": "日胜率",
        "trade_win_rate": "交易胜率",
        "profit_factor": "盈亏比",
        "n_trades": "成交笔数",
        "total_fees": "总费用(元)",
        "annual_turnover": "年化换手率",
        "benchmark_total_return": "基准累计收益",
        "benchmark_annual_return": "基准年化",
        "excess_total_return": "超额累计",
        "excess_annual_return": "超额年化",
    }
    rows: list[tuple[str, str]] = []
    for key, label in names.items():
        if key not in metrics:
            continue
        value = metrics[key]
        if key in as_pct:
            rows.append((label, f"{value * 100:.2f}%"))
        elif key in ("n_trades",):
            rows.append((label, f"{int(value)}"))
        elif key == "total_fees":
            rows.append((label, f"{value:,.2f}"))
        elif key == "profit_factor":
            rows.append((label, "∞" if value == float("inf") else f"{value:.2f}"))
        else:
            rows.append((label, f"{value:.2f}"))
    return rows
