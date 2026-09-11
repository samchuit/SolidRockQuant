"""漂移权重路径：真实模拟"买入持有→定期再平衡"的权重演化.

vectorized_backtest 的权重是"每日实际仓位比例"，因此必须让权重在再平衡日之间
随价格漂移（w[t] ∝ w[t-1]×(1+r[t-1])），否则等权常数权重 = 免费日频再平衡，
会虚增再平衡收割收益并漏记费用。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def drifted_weights(
    scores: pd.DataFrame,
    rets: pd.DataFrame,
    *,
    top_n: int | None,
    rebal_days: pd.DatetimeIndex,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """生成漂移权重宽表.

    - ``top_n`` 非 None 时按 scores 当日截面取前 top_n 等权（未入选=0）；
      None 时全体等权（无选股基线）；
    - 再平衡日之间权重按前一日收益漂移并归一（NaN 收益按 0 处理，即停牌冻结）；
    - 返回 (weights, picks_log)：weights 与 rets 同形（t 日权重作用于 t+1 收益，
      由 vectorized_backtest 内部 shift 完成），picks_log 记录每次再平衡的入选数。
    """
    symbols = list(rets.columns)
    r_np = rets.to_numpy()
    r_np = np.where(np.isnan(r_np), 0.0, r_np)
    if scores is not None:
        score_np = scores.reindex(columns=symbols).to_numpy()
    else:
        score_np = None
    n_days, n_sym = r_np.shape
    rebal_set = set(rebal_days)
    w = np.full(n_sym, np.nan)
    out = np.full((n_days, n_sym), np.nan)
    picks_log: list[tuple[pd.Timestamp, int]] = []
    dates = rets.index
    for t in range(n_days):
        d = dates[t]
        if d in rebal_set:
            if score_np is not None:
                row = score_np[t]
                valid = np.isfinite(row)
                if top_n is not None:
                    if valid.sum() < top_n:
                        pass  # 样本不足则维持原仓
                    else:
                        idx_valid = np.where(valid)[0]
                        order = idx_valid[np.argsort(-row[idx_valid])][:top_n]
                        w = np.zeros(n_sym)
                        w[order] = 1.0 / top_n
                        picks_log.append((d, top_n))
                else:
                    w = np.where(valid, 1.0 / max(valid.sum(), 1), 0.0)
                    w = w / w.sum()
                    picks_log.append((d, int(valid.sum())))
            else:
                w = np.full(n_sym, 1.0 / n_sym)
                picks_log.append((d, n_sym))
        else:
            if t > 0:
                gross = np.nansum(w * (1.0 + r_np[t - 1]))
                if gross > 0 and np.isfinite(gross):
                    w = w * (1.0 + r_np[t - 1]) / gross
        out[t] = w
    weights = pd.DataFrame(out, index=dates, columns=symbols).fillna(0.0)
    return weights, picks_log
