"""因子分析：RankIC、分层回测、ICIR 与多空价差.

方法约定（日频）：
- **RankIC**：每日截面因子值与 ``fwd_period`` 期前瞻收益的 Spearman 相关
  （前瞻收益用**后复权收盘**计算，跨除权连续）；
- **分层回测**：每日按因子值截面排名分 ``quantiles`` 层（1=最低，q=最高），
  等权持有下一期收益，逐日再平衡；
- 截面有效样本 < ``min_stocks`` 的日期跳过；因子值 NaN 不参与。

分层收益为"逐日再平衡"的快速口径，用于比较层间相对强弱，不等于真实组合回测。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from solidrock.agent.errors import ErrorCode, err
from solidrock.data.calendar import TradingCalendar
from solidrock.data.store import DataStore
from solidrock.data.symbols import validate_symbols
from solidrock.factors.base import Factor, FactorData

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass
class FactorAnalysisResult:
    """因子分析结果."""

    run_id: str
    factor_name: str
    params: dict
    config: dict
    ic: pd.Series  # RankIC 序列
    ic_summary: dict
    layer_returns: pd.DataFrame  # index=date, columns=1..quantiles
    layer_navs: pd.DataFrame
    layer_stats: pd.DataFrame  # 各层 + 多空（L-S）的绩效
    factor_autocorr: float  # 因子截面排名的自相关均值（越低换手越高）
    data_snapshot: str | None
    artifacts_dir: Path | None = None

    def summary(self) -> dict:
        return {
            "run_id": self.run_id,
            "factor": self.factor_name,
            "ic_summary": self.ic_summary,
            "layer_stats": self.layer_stats.to_dict(orient="index"),
            "artifacts_dir": str(self.artifacts_dir) if self.artifacts_dir else None,
        }


def analyze_factor(
    factor: Factor,
    store: DataStore,
    universe: Sequence[str],
    *,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    quantiles: int = 5,
    fwd_period: int = 1,
    min_stocks: int = 3,
    warmup_bars: int = 250,
    log_experiment: bool = True,
    name: str | None = None,
    notes: str | None = None,
) -> FactorAnalysisResult:
    """运行因子分析（RankIC + 分层回测），可选自动留痕."""
    if quantiles < 2:
        raise err(ErrorCode.PARAM_INVALID, f"quantiles 应 >= 2，收到 {quantiles}")
    if fwd_period < 1:
        raise err(ErrorCode.PARAM_INVALID, f"fwd_period 应 >= 1，收到 {fwd_period}")
    universe_list = [s.value for s in validate_symbols(list(universe))] if universe else []
    if not universe_list:
        raise err(
            ErrorCode.PARAM_INVALID,
            "universe 为空",
            hint="传入符号列表，如 ['000001.SZ', '600519.SH', ...]；样本股越多 IC 越可信",
        )

    # --- 日历与数据加载（含因子 lookback 的 warmup） ---
    cal = TradingCalendar(store)
    all_days = cal.days()
    start_ts, end_ts = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    if start_ts > end_ts:
        raise err(ErrorCode.PARAM_INVALID, f"区间倒置：{start_ts.date()} > {end_ts.date()}")
    eval_days = all_days[(all_days >= start_ts) & (all_days <= end_ts)]
    if len(eval_days) == 0:
        raise err(
            ErrorCode.NO_DATA,
            f"区间 {start_ts.date()}~{end_ts.date()} 内没有交易日",
            hint="检查区间或先更新交易日历",
        )
    first_pos = int(all_days.searchsorted(eval_days[0]))
    load_start = all_days[max(0, first_pos - max(warmup_bars, factor.lookback))]
    bars = store.load_bars(universe_list, start=load_start, end=end_ts)
    if bars.empty:
        raise err(
            ErrorCode.NO_DATA,
            "universe 中没有本地数据",
            hint="先执行 fetch_bars 更新 universe 的行情",
            details={"universe": universe_list[:5]},
        )

    # --- 因子值与前瞻收益 ---
    data = FactorData.from_bars(bars)
    values = factor.compute(data)
    if not isinstance(values, pd.DataFrame):
        raise err(
            ErrorCode.DATA_FORMAT_INVALID,
            f"factor.compute 应返回宽表 DataFrame，收到 {type(values).__name__}",
            hint="index=date, columns=symbol",
        )
    hfq = data.hfq_close()
    fwd_ret = hfq.shift(-fwd_period) / hfq - 1.0

    eval_index = pd.DatetimeIndex(eval_days)
    values_eval = values.reindex(eval_index)
    fwd_eval = fwd_ret.reindex(eval_index)

    # --- RankIC 序列 ---
    ic_rows: dict[pd.Timestamp, float] = {}
    for date in eval_index:
        rho = _cross_spearman(values_eval.loc[date], fwd_eval.loc[date])
        if rho is not None:
            ic_rows[date] = rho
    ic = pd.Series(ic_rows, name="rank_ic").sort_index()
    if ic.empty:
        raise err(
            ErrorCode.NO_DATA,
            "没有可用的 IC 截面（因子值与前瞻收益的有效重叠不足）",
            hint="检查因子在回测区间内是否产生有效值（lookback 是否够长、universe 是否有数据）",
        )
    ic_summary = _ic_summary(ic)

    # --- 分层回测 ---
    layer_returns = pd.DataFrame(index=eval_index, columns=list(range(1, quantiles + 1)), dtype="float64")
    groups_by_date: dict[pd.Timestamp, pd.Series] = {}
    for date in eval_index:
        row = values_eval.loc[date].dropna()
        if len(row) < min_stocks:
            continue
        pct = row.rank(pct=True)
        groups = np.ceil(pct * quantiles).clip(1, quantiles).astype(int)
        groups_by_date[date] = groups
        rets = fwd_eval.loc[date].reindex(row.index)
        for g in range(1, quantiles + 1):
            members = groups[groups == g].index
            layer_returns.loc[date, g] = rets.reindex(members).mean()
    layer_returns = layer_returns.dropna(how="all")
    if layer_returns.empty:
        raise err(
            ErrorCode.NO_DATA,
            "分层回测无有效截面",
            hint=f"截面有效样本需 >= min_stocks={min_stocks}",
        )
    layer_navs = _layer_navs(layer_returns)
    layer_stats = _layer_stats(layer_returns)
    long_short = layer_returns[quantiles] - layer_returns[1]
    layer_stats.loc["L-S"] = _perf_row(long_short.dropna())

    # 因子自相关（截面排名，衡量换手）
    autocorrs: list[float] = []
    dates_with_groups = sorted(groups_by_date)
    for prev, cur in pairwise(dates_with_groups):
        rho = _cross_spearman(groups_by_date[prev], groups_by_date[cur])
        if rho is not None:
            autocorrs.append(rho)
    factor_autocorr = float(np.mean(autocorrs)) if autocorrs else float("nan")

    result = FactorAnalysisResult(
        run_id=f"fa-{pd.Timestamp.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
        factor_name=factor.name,
        params=dict(factor.params),
        config={
            "start": str(start_ts.date()),
            "end": str(end_ts.date()),
            "universe": universe_list,
            "quantiles": quantiles,
            "fwd_period": fwd_period,
            "min_stocks": min_stocks,
            "warmup_bars": warmup_bars,
            "name": name,
            "notes": notes,
        },
        ic=ic,
        ic_summary=ic_summary,
        layer_returns=layer_returns,
        layer_navs=layer_navs,
        layer_stats=layer_stats,
        factor_autocorr=factor_autocorr,
        data_snapshot=store.snapshot,
    )
    if log_experiment:
        _log_experiment(result, store)
    return result


# ---------------------------------------------------------------------- 内部
def _cross_spearman(a: pd.Series, b: pd.Series) -> float | None:
    joined = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna()
    if len(joined) < 3:
        return None
    ra, rb = joined["a"].rank(), joined["b"].rank()
    sa, sb = ra.std(ddof=0), rb.std(ddof=0)
    if sa < 1e-12 or sb < 1e-12:
        return None
    cov = float(((ra - ra.mean()) * (rb - rb.mean())).mean())
    return cov / (float(sa) * float(sb))


def _ic_summary(ic: pd.Series) -> dict:
    n = len(ic)
    mean = float(ic.mean())
    std = float(ic.std(ddof=1)) if n > 1 else 0.0
    return {
        "ic_mean": mean,
        "ic_std": std,
        "ic_ir": mean / std if std > 1e-12 else 0.0,
        "ic_t_stat": mean / std * float(np.sqrt(n)) if std > 1e-12 else 0.0,
        "n_days": n,
        "positive_ratio": float((ic > 0).mean()),
    }


def _layer_navs(layer_returns: pd.DataFrame) -> pd.DataFrame:
    navs = (1 + layer_returns).cumprod()
    return navs.where(layer_returns.notna()).ffill().fillna(1.0)


def _perf_row(returns: pd.Series) -> pd.Series:
    r = returns.dropna()
    n = len(r)
    if n == 0:
        return pd.Series({"total_return": np.nan, "annual_return": np.nan, "sharpe": np.nan})
    total = float((1 + r).prod() - 1)
    years = n / 252
    annual = float((1 + total) ** (1 / years) - 1) if years > 0 else np.nan
    std = float(r.std(ddof=1)) if n > 1 else 0.0
    sharpe = float(r.mean() / std * np.sqrt(252)) if std > 1e-12 else 0.0
    return pd.Series({"total_return": total, "annual_return": annual, "sharpe": sharpe})


def _layer_stats(layer_returns: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({str(col): _perf_row(layer_returns[col].dropna()) for col in layer_returns.columns}).T


def _log_experiment(result: FactorAnalysisResult, store: DataStore) -> None:
    """写产物 + 实验入库（与回测的 runs/ 目录约定一致）。"""
    from solidrock.experiments.tracker import ExperimentTracker
    from solidrock.factors.report import render_factor_json, render_factor_report

    artifacts_dir = store.root / "runs" / result.run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "report.md").write_text(render_factor_report(result), encoding="utf-8")
    (artifacts_dir / "result.json").write_text(
        json.dumps(render_factor_json(result), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    result.ic.to_csv(artifacts_dir / "ic.csv")
    result.layer_navs.to_csv(artifacts_dir / "layer_navs.csv")
    result.artifacts_dir = artifacts_dir

    tracker = ExperimentTracker(store.root / "experiments.db")
    metrics = {**result.ic_summary, "factor_autocorr": result.factor_autocorr}
    tracker.log_run(
        kind="factor",
        name=result.config.get("name") or result.factor_name,
        config=result.config,
        metrics=metrics,
        artifacts_dir=str(artifacts_dir),
        data_snapshot=result.data_snapshot,
        notes=result.config.get("notes"),
        run_id=result.run_id,
    )
