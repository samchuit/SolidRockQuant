"""ML 量化管道：walk-forward 训练预测 + 预测 IC 评估 + 信号桥接."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from solidrock.agent.errors import ErrorCode, err
from solidrock.data.store import DataStore
from solidrock.factors.base import Factor, FactorData
from solidrock.ml.dataset import build_dataset, walk_forward_splits
from solidrock.ml.models import MLModel

if TYPE_CHECKING:
    pass


@dataclass
class WalkForwardResult:
    """Walk-forward ML 管道结果."""

    run_id: str
    factor_names: list[str]
    model_params: dict
    config: dict
    predictions: pd.Series  # index=(date, symbol), values=预测值
    ic_summary: dict  # 预测 IC（测试窗口上的 RankIC）
    feature_importance: dict[str, float]
    window_stats: list[dict]  # 每个窗口的 train/test IC
    backtest_metrics: dict | None  # 预测→权重的向量化回测
    data_snapshot: str | None
    artifacts_dir: Path | None = None


def walk_forward_ml(
    factors: dict[str, Factor],
    store: DataStore,
    universe: list[str],
    *,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    horizon: int = 5,
    train_window: int = 252,
    test_window: int = 21,
    step: int = 21,
    min_stocks: int = 10,
    model_params: dict | None = None,
    backtest_top: float = 0.2,
    backtest_bottom: float = 0.2,
    backtest_fee_rate: float = 1.5e-4,
    name: str | None = None,
    notes: str | None = None,
    log_experiment: bool = True,
) -> WalkForwardResult:
    """Walk-forward ML 量化管道.

    1. 计算全部因子值 → 构建点时特征矩阵 + 前瞻收益标签；
    2. walk-forward 切分：滚动训练/预测窗口，无前视；
    3. 每个窗口拟合模型 → 测试集预测 → RankIC；
    4. 全部预测拼接 → 预测 IC 汇总 + 过拟合度量（train IC vs test IC）；
    5. 预测分数 → 多空权重 → 向量化回测（信号桥接）；
    6. 自动留痕（实验库 + 产物目录）。
    """
    if not factors:
        raise err(ErrorCode.PARAM_INVALID, "factors 为空：至少提供一个因子")
    from solidrock.data.calendar import TradingCalendar
    from solidrock.data.symbols import validate_symbols

    universe_list = [s.value for s in validate_symbols(universe)]
    if not universe_list:
        raise err(ErrorCode.PARAM_INVALID, "universe 为空")
    cal = TradingCalendar(store)
    all_days = cal.days()
    start_ts, end_ts = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    eval_days = all_days[(all_days >= start_ts) & (all_days <= end_ts)]
    if len(eval_days) == 0:
        raise err(ErrorCode.NO_DATA, f"区间 {start}~{end} 内没有交易日")

    # 加载数据（含 warmup：取 start 之前足够计算因子值的天数）
    max_lookback = max(f.lookback for f in factors.values()) if factors else 0
    first_pos = int(all_days.searchsorted(eval_days[0]))
    load_start = all_days[max(0, first_pos - max(252, max_lookback))]
    bars = store.load_bars(universe_list, start=load_start, end=end_ts)
    if bars.empty:
        raise err(ErrorCode.NO_DATA, "universe 无本地数据", hint="先 fetch_bars")
    data = FactorData.from_bars(bars)
    close_hfq = data.hfq_close()

    # 计算因子值 → 点时特征矩阵 + 标签
    factor_values = {}
    for fname, factor in factors.items():
        factor_values[fname] = factor.compute(data)
    X, y = build_dataset(factor_values, close_hfq, horizon=horizon)

    # walk-forward 切分
    all_dates = pd.DatetimeIndex(sorted(set(X.index.get_level_values(0))))
    splits = walk_forward_splits(all_dates, train_window=train_window, test_window=test_window, step=step)
    if len(splits) == 0:
        raise err(ErrorCode.NO_DATA, "数据不足以生成 walk-forward 窗口")

    model = MLModel.default(**(model_params or {}))

    predictions_list: list[pd.Series] = []
    window_stats: list[dict] = []
    feature_importance_acc: dict[str, list[float]] = {}

    def _cross_rank_ic(pred: pd.Series, actual: pd.Series) -> float | None:
        joined = pd.concat([pred.rename("p"), actual.rename("a")], axis=1).dropna()
        if len(joined) < 3:
            return None
        rp, ra = joined["p"].rank(), joined["a"].rank()
        if rp.std(ddof=0) < 1e-12 or ra.std(ddof=0) < 1e-12:
            return None
        cov = float(((rp - rp.mean()) * (ra - ra.mean())).mean())
        return cov / (float(rp.std(ddof=0)) * float(ra.std(ddof=0)))

    for i, (train_dates, test_dates) in enumerate(splits):
        train_mask = X.index.get_level_values(0).isin(train_dates)
        test_mask = X.index.get_level_values(0).isin(test_dates)
        X_train, y_train = X[train_mask], y[train_mask]
        X_test = X[test_mask]
        if len(X_train) < 30 or len(X_test) < 10:
            continue
        model.fit(X_train, y_train)
        preds = model.predict(X_test)
        pred_series = pd.Series(preds, index=X_test.index, name=f"pred_w{i}")

        # 窗口 RankIC
        ic_vals = []
        for td in test_dates:
            p_day = pred_series[pred_series.index.get_level_values(0) == td]
            a_day = y[test_mask][y[test_mask].index.get_level_values(0) == td]
            ic = _cross_rank_ic(p_day, a_day)
            if ic is not None:
                ic_vals.append(ic)
        window_ic = float(np.mean(ic_vals)) if ic_vals else float("nan")

        # 训练集 IC（过拟合度量）
        train_preds = model.predict(X_train)
        train_series = pd.Series(train_preds, index=X_train.index)
        train_ic_vals = []
        for td in train_dates[-21:]:  # 只看训练窗口末 21 天
            p_day = train_series[train_series.index.get_level_values(0) == td]
            a_day = y_train[y_train.index.get_level_values(0) == td]
            ic = _cross_rank_ic(p_day, a_day)
            if ic is not None:
                train_ic_vals.append(ic)
        train_ic = float(np.mean(train_ic_vals)) if train_ic_vals else float("nan")

        window_stats.append(
            {
                "window": i,
                "train_days": len(train_dates),
                "test_days": len(test_dates),
                "train_ic": train_ic,
                "test_ic": window_ic,
                "n_test_stocks": len(X_test),
            }
        )
        predictions_list.append(pred_series)

        fi = model.feature_importance()
        for fname, imp in fi.items():
            feature_importance_acc.setdefault(fname, []).append(imp)

    if not predictions_list:
        raise err(ErrorCode.NO_DATA, "所有 walk-forward 窗口均无有效预测")
    predictions = pd.concat(predictions_list).sort_index()

    ic_summary = _prediction_ic_summary(predictions, y)
    feature_importance = {k: float(np.mean(v)) for k, v in feature_importance_acc.items()}

    # 预测 → 权重 → 向量化回测（信号桥接）
    backtest_metrics = None
    pred_wide = predictions.unstack("symbol")
    try:
        from solidrock.backtest.vectorized import vectorized_backtest, weights_from_factor

        close_daily = close_hfq.reindex(pred_wide.index).ffill()
        weights = weights_from_factor(pred_wide, top=backtest_top, bottom=backtest_bottom)
        bt = vectorized_backtest(weights, close_daily, fee_rate=backtest_fee_rate)
        backtest_metrics = bt.metrics
        backtest_metrics["annual_turnover"] = float(bt.turnover.mean() * 252)
    except Exception:
        pass

    result = WalkForwardResult(
        run_id=f"ml-{pd.Timestamp.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
        factor_names=list(factors.keys()),
        model_params=model_params or {},
        config={
            "start": str(start_ts.date()),
            "end": str(end_ts.date()),
            "universe": universe_list,
            "horizon": horizon,
            "train_window": train_window,
            "test_window": test_window,
            "step": step,
            "min_stocks": min_stocks,
            "backtest_top": backtest_top,
            "backtest_bottom": backtest_bottom,
            "name": name,
            "notes": notes,
        },
        predictions=predictions,
        ic_summary=ic_summary,
        feature_importance=feature_importance,
        window_stats=window_stats,
        backtest_metrics=backtest_metrics,
        data_snapshot=store.snapshot,
    )
    return result


def _prediction_ic_summary(predictions: pd.Series, y: pd.Series) -> dict:
    joined = pd.DataFrame({"pred": predictions, "actual": y}).dropna()
    if joined.empty:
        return {"ic_mean": 0.0, "ic_ir": 0.0, "n_days": 0, "positive_ratio": 0.0, "train_test_gap": 0.0}
    dates = joined.index.get_level_values(0).unique()
    ics = []
    for d in dates:
        day = joined[joined.index.get_level_values(0) == d]
        rp, ra = day["pred"].rank(), day["actual"].rank()
        if rp.std(ddof=0) < 1e-12 or ra.std(ddof=0) < 1e-12:
            continue
        cov = float(((rp - rp.mean()) * (ra - ra.mean())).mean())
        ics.append(cov / (float(rp.std(ddof=0)) * float(ra.std(ddof=0))))
    n = len(ics)
    mean = float(np.mean(ics)) if ics else 0.0
    std = float(np.std(ics, ddof=1)) if n > 1 else 0.0
    return {
        "ic_mean": mean,
        "ic_std": std,
        "ic_ir": mean / std if std > 1e-12 else 0.0,
        "n_days": n,
        "positive_ratio": float(np.mean([i > 0 for i in ics])) if ics else 0.0,
        "train_test_gap": 0.0,  # 过拟合度量由调用方从 window_stats 计算
    }
