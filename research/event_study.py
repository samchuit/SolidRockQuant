"""业绩预告事件研究：公告效应与事件后漂移（A股，超额对标中证1000指数）.

事件窗：
- [A-1, A+1]：公告脉冲（A 为公告日，A-1 为抢跑日）
- [A+1, A+21]：事件后 20 交易日内漂移（预告效应）

宇宙：中证1000 当前成分（幸存者偏差存在，见总报告 §5——对"预增"类偏乐观、
对"首亏/预减"类偏悲观，读表时注意方向性）。
输出: research/event_results.csv
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from solidrock.data.store import DataStore  # noqa: E402

STORE = DataStore(ROOT / ".solidrock")
CONS = ROOT / "research" / "all_market_stocks.csv"


def window_ret(prices: pd.Series, ann: pd.Timestamp, a: int, b: int) -> float | None:
    """事件窗收益：prices 为日频序列；窗口为相对公告日的交易日偏移."""
    idx = prices.index
    pos = idx.searchsorted(ann)
    if pos + b >= len(idx) or pos + a < 0:
        return None
    if pos > 0 and (idx[pos] - ann).days > 15:  # 公告日停牌等，放宽到下一交易日
        pass
    p_a = prices.iloc[pos + a]
    p_b = prices.iloc[pos + b]
    if not (np.isfinite(p_a) and np.isfinite(p_b)) or p_a <= 0:
        return None
    return p_b / p_a - 1.0


def main() -> None:
    ev = pd.read_parquet(ROOT / "research" / "events" / "yjyg.parquet")
    ev["code"] = ev["股票代码"].astype(str).str.zfill(6)
    ev["ann"] = pd.to_datetime(ev["公告日期"])
    ev = ev.dropna(subset=["ann", "预告类型"])
    # 只保留净利润指标的最近一条（同股同期同日可能多条指标）
    ev = ev[ev["预测指标"].str.contains("净利润", na=False)]
    ev = ev.sort_values("ann").drop_duplicates(["code", "ann"], keep="last")

    cons = pd.read_csv(CONS, dtype={"symbol": str})
    universe = set(cons["symbol"])
    ev = ev[ev["code"].map(lambda c: f"{c}.SH" in universe or f"{c}.SZ" in universe)].copy()
    ev["symbol"] = ev["code"].map(lambda c: f"{c}.SH" if c.startswith(("6",)) else f"{c}.SZ")
    print(f"宇宙内事件: {len(ev)} 条, 股票 {ev['symbol'].nunique()} 只")

    # 载入价格面板（hfq）
    symbols = sorted(ev["symbol"].unique())
    bars = STORE.load_bars(symbols, start="2015-06-01", end="2026-09-09")
    hfq = (bars.pivot_table(index="date", columns="symbol", values="close", aggfunc="last")
           * bars.pivot_table(index="date", columns="symbol", values="adj_factor", aggfunc="last")).sort_index()
    idx = STORE.load_bars("000852.SH", start="2015-06-01", end="2026-09-09").set_index("date")["close"]
    idx.index = pd.to_datetime(idx.index)

    rows = []
    for _, r in ev.iterrows():
        sym = r["symbol"]
        if sym not in hfq.columns:
            continue
        prices = hfq[sym].dropna()
        if len(prices) < 60:
            continue
        ann = r["ann"]
        if ann < prices.index[0] or ann > prices.index[-1] - pd.Timedelta(days=40):
            continue
        # 指数同窗
        ret_s1 = window_ret(prices, ann, -1, 1)
        ret_d20 = window_ret(prices, ann, 1, 21)
        ret_i1 = window_ret(idx, ann, -1, 1)
        ret_i20 = window_ret(idx, ann, 1, 21)
        if None in (ret_s1, ret_d20, ret_i1, ret_i20):
            continue
        rows.append({
            "code": r["code"], "symbol": sym, "ann": ann, "type": r["预告类型"],
            "year": ann.year, "report": r["报告期"],
            "car_3d": (1 + ret_s1) / (1 + ret_i1) - 1,
            "car_20d": (1 + ret_d20) / (1 + ret_i20) - 1,
        })
    res = pd.DataFrame(rows)
    print(f"有效事件: {len(res)}")
    g = res.groupby("type").agg(
        n=("car_3d", "size"),
        car3_mean=("car_3d", "mean"),
        car3_med=("car_3d", "median"),
        car3_win=("car_3d", lambda s: (s > 0).mean()),
        car20_mean=("car_20d", "mean"),
        car20_med=("car_20d", "median"),
        car20_win=("car_20d", lambda s: (s > 0).mean()),
    ).sort_values("car20_mean", ascending=False)
    print("\n=== 按预告类型的事件窗超额（CAR，超中证1000）===")
    print(g.round(4).to_string())
    g.to_csv(ROOT / "research" / "event_results_by_type.csv", encoding="utf-8-sig")

    by_year = res.groupby(["year", "type"])["car_20d"].mean().unstack()
    by_year.round(3).to_csv(ROOT / "research" / "event_results_by_year.csv", encoding="utf-8-sig")
    print("\n=== car_20d 分年（预增/预减 两列）===")
    cols = [c for c in ("预增", "预减", "首亏", "扭亏") if c in by_year.columns]
    print(by_year[cols].round(3).to_string())


if __name__ == "__main__":
    main()
