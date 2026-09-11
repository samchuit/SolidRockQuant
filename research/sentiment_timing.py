"""情绪/资金面择时研究：全市场统计指标 → 指数择时实证.

指标（全部为 t 日收盘可得，无前视）：
- limit_up_n:     涨停家数（按板块涨跌停幅度判定，close==high 且涨幅达标）
- up_ratio:       上涨家数占比
- med_ret:        全市场日收益中位数（20日和，赚钱效应）
- new_high_20:    创20日新高家数占比
- amount_z:       总成交额 20 日 Z 分
- north_z:        北向当日净买额 20 日和 Z 分（2014-11~可得区间）
- margin_chg:     两融余额 20 日变化率（沪+深）

实证：
1) RankIC：各指标 vs 沪深300 / 中证1000 未来 5/20 日收益；
2) 分位组合：指标五分位 → 下一期指数平均收益（单调性）；
3) 择时叠加：指标>0 满仓 / 否则空仓（含20日均线平滑版），vs 买入持有。
输出: research/sentiment_results.csv + 控制台表
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
START, END = "2016-06-01", "2026-09-09"


def limit_ratio(code: str) -> float:
    if code.startswith(("30", "68")):
        return 1.20
    return 1.10


def build_market_stats() -> pd.DataFrame:
    """遍历全市场 parquet，聚合日度统计（省内存：逐文件处理）."""
    files = sorted((STORE.bars_dir("1d")).glob("*.parquet"))
    close_p, pre_p, high_p, amt_p = {}, {}, {}, {}
    n_used = 0
    for f in files:
        sym = f.stem.replace(".parquet", "")
        if not sym.split(".")[0].startswith(("00", "30", "60", "68")):
            continue
        try:
            df = STORE.load_bars(sym, start="2015-06-01", end=END, columns=["date", "close", "pre_close", "high", "amount"])
        except Exception:  # noqa: BLE001
            continue
        if df.empty:
            continue
        df = df.set_index("date")
        close_p[sym] = df["close"]
        pre_p[sym] = df["pre_close"]
        high_p[sym] = df["high"]
        amt_p[sym] = df["amount"]
        n_used += 1
    print(f"载入 {n_used} 只股票")
    close = pd.DataFrame(close_p).sort_index()
    pre = pd.DataFrame(pre_p).reindex(close.index)
    high = pd.DataFrame(high_p).reindex(close.index)
    amount = pd.DataFrame(amt_p).reindex(close.index)

    ret = close / pre - 1.0
    ratios = pd.Series({c: limit_ratio(c) for c in close.columns})
    limit_up = (close >= pre * ratios * 0.998) & (close == high)
    new_high_20 = close >= close.rolling(20).max()

    stats = pd.DataFrame({
        "limit_up_n": limit_up.sum(axis=1),
        "up_ratio": (ret > 0).sum(axis=1) / ret.notna().sum(axis=1),
        "med_ret": ret.median(axis=1),
        "new_high_20": new_high_20.sum(axis=1) / close.notna().sum(axis=1),
        "amount": amount.sum(axis=1),
    }, index=close.index)
    stats = stats.loc[START:END]
    # 平滑/标准化
    stats["amount_z"] = ((np.log(stats["amount"]) - np.log(stats["amount"]).rolling(20).mean())
                         / np.log(stats["amount"]).rolling(20).std())
    stats["med_ret_20"] = stats["med_ret"].rolling(20).sum()
    return stats


def load_extra() -> pd.DataFrame:
    """北向净买额与两融余额变化率，按日对齐."""
    ff = ROOT / "research" / "fundflow"
    extra = pd.DataFrame(index=stats_index_holder["idx"])
    try:
        north = pd.read_csv(ff / "north.csv")
        north.columns = ["date", "netbuy"] + list(north.columns[2:])
        north["date"] = pd.to_datetime(north["date"])
        s = north.set_index("date")["netbuy"].astype(float).sort_index()
        s20 = s.rolling(20).sum()
        extra["north_z"] = ((s20 - s20.rolling(120).mean()) / s20.rolling(120).std()).reindex(extra.index)
    except Exception as e:  # noqa: BLE001
        print("北向载入失败:", e)
    try:
        sh = pd.read_csv(ff / "margin_sh.csv"); sz = pd.read_csv(ff / "margin_sz.csv")
        def mbalance(df: pd.DataFrame) -> pd.Series:
            dcol = [c for c in df.columns if "日期" in c][0]
            vcol = [c for c in df.columns if "融资余额" in c][0]
            s = df[[dcol, vcol]].copy()
            s[dcol] = pd.to_datetime(s[dcol])
            return s.set_index(dcol)[vcol].astype(float).sort_index()
        m = (mbalance(sh) + mbalance(sz)).dropna()
        mchg = m.pct_change(20)
        extra["margin_chg"] = mchg.reindex(extra.index)
    except Exception as e:  # noqa: BLE001
        print("两融载入失败:", e)
    return extra


stats_index_holder = {"idx": None}


def rank_ic(ind: pd.Series, fwd: pd.Series) -> tuple[float, float]:
    df = pd.DataFrame({"x": ind, "y": fwd}).dropna()
    if len(df) < 100:
        return np.nan, np.nan
    ic = df["x"].corr(df["y"], method="spearman")
    t = ic / (df["x"].corr(df["y"], method="spearman") ** 0 + 1)  # 占位
    # t 值用滚动 IC 标准误近似
    ics = pd.Series(df.index).astype(str)
    _ = ics
    icir = df.groupby(df.index // 20).apply(lambda g: g["x"].corr(g["y"], method="spearman")).mean()
    return ic, icir


def main() -> None:
    stats = build_market_stats()
    stats_index_holder["idx"] = stats.index
    extra = load_extra()
    stats = stats.join(extra)

    idx_close = {}
    for sym, label in (("000300.SH", "HS300"), ("000852.SH", "CSI1000")):
        idx_close[label] = STORE.load_bars(sym, start=START, end=END).set_index("date")["close"]
    idx_close = pd.DataFrame(idx_close).reindex(stats.index).ffill()

    rows = []
    fwd_map = {}
    for label in idx_close.columns:
        r = idx_close[label].pct_change()
        fwd_map[(label, 5)] = (idx_close[label].shift(-5) / idx_close[label] - 1)
        fwd_map[(label, 20)] = (idx_close[label].shift(-20) / idx_close[label] - 1)

    indicators = {
        "limit_up_n": stats["limit_up_n"],
        "limit_up_z": (stats["limit_up_n"] - stats["limit_up_n"].rolling(60).mean()) / stats["limit_up_n"].rolling(60).std(),
        "up_ratio": stats["up_ratio"],
        "med_ret_20": stats["med_ret_20"],
        "new_high_20": stats["new_high_20"],
        "amount_z": stats["amount_z"],
        "north_z": stats["north_z"],
        "margin_chg": stats["margin_chg"],
    }
    for iname, ind in indicators.items():
        if ind is None or ind.notna().sum() < 200:
            continue
        row = {"indicator": iname}
        for (label, h), fwd in fwd_map.items():
            common = ind.dropna().index.intersection(fwd.dropna().index)
            if len(common) < 200:
                continue
            ic = ind.reindex(common).corr(fwd.reindex(common), method="spearman")
            row[f"ic_{label}_{h}d"] = ic
        rows.append(row)
    res = pd.DataFrame(rows).set_index("indicator")
    out = ROOT / "research" / "sentiment_results.csv"
    res.to_csv(out, encoding="utf-8-sig")
    print("=== 情绪/资金面指标 RankIC（vs 指数未来收益）===")
    print(res.round(4).to_string())
    print(f"\n已写出: {out}")


if __name__ == "__main__":
    main()
