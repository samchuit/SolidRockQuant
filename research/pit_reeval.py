"""PIT（point-in-time）无偏宇宙复评：等权基线 / REV20 / SIZE，对比幸存者偏差版.

PIT 宇宙：t 日的成分 = 最新一期月度权重快照（trade_date ≤ t）的成分集。
对照口径与偏差版完全一致（月调、漂移权重、单边 0.15%）。
输出: research/pit_reeval_results.csv
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "csi1000"))

from solidrock.backtest.vectorized import vectorized_backtest  # noqa: E402
from solidrock.data.store import DataStore  # noqa: E402
from weights_drift import drifted_weights  # noqa: E402

STORE = DataStore(ROOT / ".solidrock")
START, END = "2016-06-01", "2026-09-09"
FEE = 0.0015


def main() -> None:
    w_snap = pd.read_parquet(ROOT / "research" / "tushare" / "index_weight_000852.parquet")
    w_snap["trade_date"] = pd.to_datetime(w_snap["trade_date"])
    snaps = w_snap.sort_values("trade_date")
    snap_dates = snaps["trade_date"].unique()

    members_all = sorted(snaps["con_code"].unique())
    print(f"历史成分合计: {len(members_all)} 只")

    bars = STORE.load_bars(members_all, start="2015-06-01", end=END).sort_values(["symbol", "date"])
    hfq = (bars.pivot_table(index="date", columns="symbol", values="close", aggfunc="last")
           * bars.pivot_table(index="date", columns="symbol", values="adj_factor", aggfunc="last")).sort_index()
    ret = hfq.pct_change()
    dates = hfq.index
    members_all = [c for c in members_all if c in hfq.columns]  # 无价格数据的成员剔除
    snap_dates = snap_dates

    def pit_universe(d: pd.Timestamp) -> set[str]:
        """t 日的 PIT 成分：最新一期（trade_date ≤ t）权重快照的成分集."""
        avail = snap_dates[snap_dates <= d]
        if len(avail) == 0:
            return set()
        snap = avail[-1]
        return set(snaps.loc[snaps["trade_date"] == snap, "con_code"])

    # 月末再平衡日（用 PIT 宇宙）
    month_ends = dates[dates.is_month_end]
    rebal_days = month_ends[(month_ends >= pd.Timestamp(START)) & (month_ends <= pd.Timestamp(END))]

    def pit_weights(scores_fn=None, top_n=None) -> pd.DataFrame:
        """逐月 PIT 漂移权重：每次调仓只在**当期成分**内选股，成分外权重归零.

        与 ``weights_drift`` 同逻辑（真实漂移权重，非常数权重），区别是宇宙逐月
        取当期的指数成分——这才叫 PIT。此前实现直接在全历史成分并集
        （``members_all``）里选股，等于没有成分约束，标题里的 "PIT" 名不副实。
        """
        w_np = np.full((len(dates), len(members_all)), np.nan)
        r_np = np.where(np.isnan(ret.to_numpy()), 0.0, ret.to_numpy())
        col_index = {s: i for i, s in enumerate(members_all)}
        rebal = set(rebal_days)
        uni_cache: dict[pd.Timestamp, list[str]] = {}
        for t, d in enumerate(dates):
            if d in rebal:
                if d not in uni_cache:
                    uni = pit_universe(d)
                    uni_cache[d] = [
                        s
                        for s in uni
                        if s in col_index and s in hfq.columns and d in hfq.index and pd.notna(hfq.at[d, s])
                    ]
                members_today = uni_cache[d]
                if not members_today:
                    w_np[t] = w_np[t - 1] if t > 0 else 0.0
                    continue
                if scores_fn is not None:
                    row = scores_fn.reindex(index=[d], columns=members_today).iloc[0].dropna()
                    if len(row) < (top_n or 1):
                        w_np[t] = w_np[t - 1] if t > 0 else 0.0
                        continue
                    sel = list(row.index) if top_n is None else list(row.nlargest(top_n).index)
                else:
                    sel = members_today
                w_np[t] = 0.0
                weight = 1.0 / len(sel)
                for s in sel:
                    w_np[t, col_index[s]] = weight
            elif t > 0:
                gross = np.nansum(w_np[t - 1] * (1 + r_np[t - 1]))
                if gross > 0:
                    w_np[t] = w_np[t - 1] * (1 + r_np[t - 1]) / gross
        return pd.DataFrame(w_np, index=dates, columns=members_all).fillna(0.0)

    out = {}

    # 1) 等权基线 PIT（无选股）
    wp = pit_weights(None, None)
    res = vectorized_backtest(wp, hfq, fee_rate=FEE)
    out["EW_PIT"] = {"annual": res.metrics["annual_return"], "max_dd": res.metrics["max_drawdown"]}

    # 2) REV20 PIT
    rev = -(hfq / hfq.shift(20) - 1.0)
    rp = pit_weights(rev, 100)
    res = vectorized_backtest(rp, hfq, fee_rate=FEE)
    out["REV20_top100_PIT"] = {"annual": res.metrics["annual_return"], "max_dd": res.metrics["max_drawdown"]}

    # 3) SIZE PIT（负log市值不做了——无历史市值；用对偶：当前成分外的不可比，跳过）
    res_df = pd.DataFrame(out).T
    res_df.to_csv(ROOT / "research" / "pit_reeval_results.csv", encoding="utf-8-sig")
    print("\n=== PIT 无偏宇宙 vs 幸存者偏差版 ===")
    print(f"等权基线 PIT:     年化={res_df.loc['EW_PIT','annual']:+.2%} 回撤={res_df.loc['EW_PIT','max_dd']:.1%}   （偏差版 11.63%/-30.6%）")
    print(f"REV20 top100 PIT: 年化={res_df.loc['REV20_top100_PIT','annual']:+.2%} 回撤={res_df.loc['REV20_top100_PIT','max_dd']:.1%}   （偏差版 11.70%/-59.5%）")


if __name__ == "__main__":
    main()
