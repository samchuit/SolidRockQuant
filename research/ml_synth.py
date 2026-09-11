"""ML 因子合成：LightGBM 滚动训练（严格样本外，月度截面）.

特征（t 日截面 rank，全部已验证可得）：REV20, VOL20, AMP20, MOM120, BP, EP, SIZE, ILLIQ20
标签：t→t+20 日收益 - **当期 PIT 宇宙**等权同期收益（超额）
训练：年份 Y-1 及以前 → 预测 Y 年各月末截面；OOS = 2019~2026 逐年
评估：OOS RankIC（按月）、Top-100 组合月度收益 vs 等权基线（漂移权重口径另在组合脚本）
防泄露：特征仅用 t 日信息；训练集不含预测年；**宇宙逐期取 PIT 成分**（非当前成分）。

**PIT 修正说明**：此前版本用"当前 1000 只成分"训练与回测，等于把后来纳入的
赢家提前放进宇宙、把退市/剔除的输家排除，是幸存者偏差；且特征含 SIZE/ILLIQ
这类偏差暴露最重的变量，报告的 23.5% 年化不可作为有效收益证据。本版改为：
1) 宇宙 = 历史权重快照的成分并集（含 137 只退市股）；2) 样本只保留当期 PIT 成分；
3) 标签超额以当期 PIT 宇宙为基准；4) 回测选股仅在 PIT 成分内。

特征取舍：``EP``/``SIZE`` 依赖总市值，而市值序列只有当前成分（每只一个
``mktcap_*.parquet``），对历史非当前成分缺失，故从特征集中移除，避免"缺失恰好
集中在偏差相关子集"造成的误导。保留 6 个：REV20/VOL20/AMP20/MOM120/ILLIQ20/BP。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CSI = Path(__file__).resolve().parent / "csi1000"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CSI))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pit_universe import load_snapshots, pit_membership  # noqa: E402
from portfolio_backtest import factor_panel, load_panels  # noqa: E402
from weights_drift import drifted_weights  # noqa: E402

from solidrock.backtest.vectorized import vectorized_backtest  # noqa: E402
from solidrock.data.store import DataStore  # noqa: E402

STORE = DataStore(ROOT / ".solidrock")
ENDS = "2026-09-08"
# 不含 EP/SIZE（市值仅覆盖当前成分，见模块 docstring）
FEATS = ["REV20", "VOL20", "AMP20", "MOM120", "ILLIQ20", "BP"]
H = 20


def pit_union_universe() -> list[str]:
    """历史权重快照的成分并集（含退市股），仅在本地有行情者保留."""
    snaps = load_snapshots()
    union = sorted(snaps["con_code"].unique())
    bars = STORE.load_bars(union, start="2015-01-01", end=ENDS, columns=["date", "symbol"])
    available = set(bars["symbol"].unique())
    return [s for s in union if s in available]


def main() -> None:
    import lightgbm as lgb

    from fund_factor_research import build_factor_panels

    universe = pit_union_universe()
    print(f"PIT 宇宙（含退市）: {len(universe)} 只")

    data = load_panels(universe)
    hfq = data.hfq_close()
    ret = hfq.pct_change()

    # --- PIT 宇宙掩码：逐期成分，替代"当前成分"口径 ---
    mask = pit_membership(hfq.index, hfq.columns)
    print(f"PIT 掩码：{mask.index[0].date()}~{mask.index[-1].date()}，"
          f"平均每期成分 {mask.sum(axis=1).mean():.0f} 只")

    fwd = hfq.shift(-H) / hfq - 1.0
    fwd = fwd.where(mask)  # 非当期成分不参与
    fwd_excess = fwd.sub(fwd.mean(axis=1), axis=0)  # 当期 PIT 宇宙内超额

    fund_panels, fund_hfq, _ = build_factor_panels(universe)

    X = {}
    for f in FEATS:
        if f in fund_panels:  # BP/EP/SIZE/ROE/QUAL/VG 等基本面面板
            X[f] = fund_panels[f]
            continue
        try:
            X[f] = factor_panel(f, data).rank(axis=1, pct=True)
        except Exception:
            pass
    fx = None
    for f, panel in X.items():
        s = panel.stack().rename(f).reset_index()
        s.columns = ["date", "symbol", f]
        s["date"] = pd.to_datetime(s["date"])
        fx = s if fx is None else fx.merge(s, on=["date", "symbol"], how="inner")
    for c in FEATS:  # 截面标准化（基本面面板为原始值）
        fx[c] = fx.groupby("date")[c].transform(lambda g: (g - g.mean()) / g.std())
    lb = fwd_excess.stack().rename("y").reset_index()
    lb.columns = ["date", "symbol", "y"]
    lb["date"] = pd.to_datetime(lb["date"])
    df = fx.merge(lb, on=["date", "symbol"]).dropna()
    # 只保留当期 PIT 成分（fx 由全历史成分构成，此处剔除当期非成分）
    mask_long = mask.stack().rename("pit").reset_index()
    mask_long.columns = ["date", "symbol", "pit"]
    mask_long["date"] = pd.to_datetime(mask_long["date"])
    df = df.merge(mask_long, on=["date", "symbol"], how="left")
    df = df[df["pit"].fillna(False)].drop(columns=["pit"])
    df = df.set_index(["date", "symbol"])
    df["year"] = df.index.get_level_values(0).year
    # 只用月末截面（月度调仓对应，训练量 117 月 × ~1000）
    df = df[df.index.get_level_values(0).is_month_end]
    print(f"训练样本: {len(df)} 行, 特征 {len(FEATS)} 个")

    oos_preds = []
    years = sorted(df["year"].unique())
    for y in years[2:]:  # 至少2年训练
        tr = df[df["year"] < y]
        te = df[df["year"] == y]
        if len(tr) < 5000 or len(te) == 0:
            continue
        model = lgb.LGBMRegressor(n_estimators=200, learning_rate=0.05, num_leaves=31,
                                  subsample=0.8, colsample_bytree=0.8, min_child_samples=100,
                                  verbose=-1, random_state=7)
        model.fit(tr[FEATS], tr["y"])
        pred = pd.Series(model.predict(te[FEATS]), index=te.index, name="pred")
        oos_preds.append(pred)
        ic = pred.groupby(level=0).apply(lambda s: s.droplevel(0).corr(
            te.loc[s.index, "y"], method="spearman")).mean()
        print(f"{y}: OOS 月均 RankIC = {ic:+.4f}")
        del model
    oos = pd.concat(oos_preds)
    oos.to_csv(ROOT / "research" / "ml_oos_preds.csv", encoding="utf-8-sig")

    # OOS Top-100 月调组合（选股限制在当期 PIT 成分内：非成分打分置 NaN）
    wide = oos.reset_index().pivot_table(index="date", columns="symbol", values="pred")
    wide.index = pd.to_datetime(wide.index)
    wide = wide.reindex(columns=ret.columns)
    wide = wide.where(mask.reindex(index=wide.index, columns=wide.columns).fillna(False))
    w, _ = drifted_weights(wide.reindex(ret.index), ret, top_n=100, rebal_days=wide.index)
    res = vectorized_backtest(w, hfq, fee_rate=0.0015)
    m = res.metrics
    print(f"\nML Top-100（PIT 宇宙、OOS 月调）: 年化={m['annual_return']:.2%} 回撤={m['max_drawdown']:.2%}")

    # 同口径 PIT 等权基线（无选股），作为超额的正确对照
    base_w, _ = drifted_weights(None, ret.where(mask), top_n=None, rebal_days=wide.index)
    base = vectorized_backtest(base_w, hfq, fee_rate=0.0015).metrics
    print(f"PIT 等权基线（OOS 月调）:      年化={base['annual_return']:.2%} 回撤={base['max_drawdown']:.2%}")
    print("（注意：基线须同口径才可比；此前用『当前成分』的 11.63% 对照是不同宇宙，不可比）")


if __name__ == "__main__":
    main()
