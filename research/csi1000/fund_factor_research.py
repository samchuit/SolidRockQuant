"""基本面因子研究（中证1000 成分，point-in-time 公告日对齐）.

数据:
- yjbb.parquet: 东财业绩报表长表（报告期/公告日期/ROE/毛利率/增速/净利润/BVPS）
- mktcap_{code}.parquet: 百度总市值序列

因子（全部"公告日之后才可见"，无前视）:
- ROE:    最新披露的净资产收益率（累计口径）
- NPG:    净利润同比增长
- SRG:    营业总收入同比增长
- GPM:    销售毛利率
- EP:     TTM净利润 / 总市值（盈利收益率）
- BP:     每股净资产 / 收盘价（账面市值比）
- SIZE:   -log(总市值)（小盘，注意与幸存者偏差同向，见报告）
- QUAL:   ROE 与毛利率的 rank 均值（质量）
- VG:     NPG 与 SRG 的 rank 均值（成长）

分析: 日频 RankIC + 五分层（与量价研究同口径，fwd=1 日）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from solidrock.data.store import DataStore  # noqa: E402

ROOT_P = Path(__file__).parent
STORE = DataStore(ROOT / ".solidrock")
START, END = "2017-01-03", "2026-09-08"


def load_bars_panel(universe: list[str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(hfq_close, close, ret) 宽表。``universe=None`` 用当前成分（含幸存者偏差）."""
    if universe is None:
        universe = pd.read_csv(ROOT_P / "cons_current.csv")["symbol"].tolist()
    bars = STORE.load_bars(universe, start="2015-06-01", end=END)
    hfq = bars.pivot_table(index="date", columns="symbol", values="close", aggfunc="last") * \
        bars.pivot_table(index="date", columns="symbol", values="adj_factor", aggfunc="last")
    close = bars.pivot_table(index="date", columns="symbol", values="close", aggfunc="last")
    return hfq, close, hfq.pct_change()


def load_fundamentals() -> pd.DataFrame:
    df = pd.read_parquet(ROOT_P / "fundamentals" / "yjbb.parquet")
    df = df.rename(columns={"股票代码": "code", "报告期": "period", "最新公告日期": "ann_date",
                            "净资产收益率": "roe", "销售毛利率": "gpm",
                            "净利润-同比增长": "npg", "营业总收入-同比增长": "srg",
                            "净利润-净利润": "profit", "每股净资产": "bvps"})
    df["ann_date"] = pd.to_datetime(df["ann_date"], errors="coerce")
    df["code"] = df["code"].astype(str).str.zfill(6)
    df = df.dropna(subset=["ann_date"])
    # 同一(股票,报告期)可能有多条（快报/更正），取公告日最新一条
    df = df.sort_values("ann_date").groupby(["code", "period"], as_index=False).last()
    return df


def asof_panel(fund: pd.DataFrame, col: str, dates: pd.DatetimeIndex, symbols: list[str]) -> pd.DataFrame:
    """公告日对齐: 每只股票按公告日排序后 asof 到交易日（最新已披露值）."""
    out = pd.DataFrame(np.nan, index=dates, columns=symbols)
    sub = fund.dropna(subset=[col])[["code", "ann_date", col]].sort_values(["code", "ann_date"])
    by_code = {c: g.set_index("ann_date")[col] for c, g in sub.groupby("code")}
    sym_by_code = {s.split(".")[0]: s for s in symbols}
    for code, series in by_code.items():
        sym = sym_by_code.get(code)
        if sym is None:
            continue
        series = series[~series.index.duplicated(keep="last")]
        aligned = series.reindex(series.index.union(dates)).ffill().reindex(dates)
        out[sym] = aligned.to_numpy()
    return out


def ttm_profit(fund: pd.DataFrame) -> pd.DataFrame:
    """TTM 净利润: 累计值差分为单季，再滚动 4 季求和（按公告日 asof 输出）."""
    f = fund.dropna(subset=["profit"]).sort_values(["code", "period"]).copy()
    f["q"] = f["period"].dt.quarter
    f["year"] = f["period"].dt.year
    f["cum"] = f["profit"]
    rows = []
    for code, g in f.groupby("code"):
        g = g.sort_values("period")
        cum = g.set_index("period")["cum"]
        # 单季 = 累计 - 上季累计（同年）
        single = cum.copy()
        for i in range(1, len(g)):
            p = g.iloc[i]
            prev = g.iloc[i - 1]
            if p["year"] == prev["year"] and p["q"] == prev["q"] + 1:
                single.loc[p["period"]] = p["cum"] - prev["cum"]
        ttm = single.rolling(4, min_periods=4).sum()
        rows.append(pd.DataFrame({"code": code, "period": ttm.index, "profit_ttm": ttm.values}))
    q = pd.concat(rows, ignore_index=True)
    ann = fund.dropna(subset=["profit"]).groupby(["code", "period"])["ann_date"].max().reset_index()
    q = q.merge(ann, on=["code", "period"], how="left")
    return q.sort_values(["code", "ann_date"])


def build_factor_panels(
    universe: list[str] | None = None,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, pd.DataFrame]:
    """因子面板 + (hfq_close, ret)。``universe`` 可传 PIT 全历史成分以消除幸存者偏差.

    注意：市值仅有当前成分的序列（``mktcap_*.parquet`` 每只一个文件），因此
    传入 PIT 宇宙时 **EP/SIZE 对历史非当前成分全为 NaN**，引擎/模型需自行处理缺失。
    """
    hfq, close, ret = load_bars_panel(universe)
    dates = hfq.index
    symbols = list(hfq.columns)
    fund = load_fundamentals()
    # 市值宽表
    mc_files = sorted((ROOT_P / "fundamentals").glob("mktcap_*.parquet"))
    mc = pd.concat([pd.read_parquet(f) for f in mc_files], ignore_index=True) if mc_files else pd.DataFrame()
    mktcap = pd.DataFrame(np.nan, index=dates, columns=symbols)
    if not mc.empty:
        sym_by_code = {s.split(".")[0]: s for s in symbols}
        for code, g in mc.groupby("code"):
            sym = sym_by_code.get(str(code))
            if sym is None:
                continue
            g = g.set_index("date")["mktcap"].sort_index()
            aligned = g.reindex(g.index.union(dates)).ffill().reindex(dates)
            mktcap[sym] = aligned.to_numpy()
    # asof 基本面
    roe = asof_panel(fund, "roe", dates, symbols)
    npg = asof_panel(fund, "npg", dates, symbols)
    srg = asof_panel(fund, "srg", dates, symbols)
    gpm = asof_panel(fund, "gpm", dates, symbols)
    bvps = asof_panel(fund, "bvps", dates, symbols)
    ttm = ttm_profit(fund)
    profit_ttm = asof_panel(ttm.rename(columns={"profit_ttm": "v"}), "v", dates, symbols)

    ep = profit_ttm / (mktcap * 1e8)  # 百度市值单位为亿元
    bp = bvps / close.reindex(index=dates, columns=symbols)
    size = -np.log(mktcap)
    quality = (roe.rank(axis=1, pct=True) + gpm.rank(axis=1, pct=True)) / 2
    growth = (npg.rank(axis=1, pct=True) + srg.rank(axis=1, pct=True)) / 2

    panels = {"ROE": roe, "NPG": npg, "SRG": srg, "GPM": gpm, "EP": ep, "BP": bp,
              "SIZE": size, "QUAL": quality, "VG": growth}
    return panels, hfq, ret


def rank_ic(values: pd.DataFrame, fwd_ret: pd.DataFrame) -> pd.Series:
    """日频 Spearman RankIC（截面秩相关）."""
    ics = {}
    common = values.index.intersection(fwd_ret.index)
    for d in common:
        x = values.loc[d]
        y = fwd_ret.loc[d]
        mask = x.notna() & y.notna()
        if mask.sum() < 100:
            continue
        ics[d] = x[mask].corr(y[mask], method="spearman")
    return pd.Series(ics).sort_index()


def layer_returns(values: pd.DataFrame, fwd_ret: pd.DataFrame, q: int = 5) -> pd.DataFrame:
    """分层等权收益（日频换组）."""
    layers = pd.DataFrame(index=values.index, columns=range(1, q + 1), dtype=float)
    for d in values.index:
        row = values.loc[d].dropna()
        if len(row) < 100:
            continue
        pct = row.rank(pct=True)
        grp = np.ceil(pct * q).clip(1, q).astype(int)
        y = fwd_ret.loc[d]
        for g in range(1, q + 1):
            members = grp[grp == g].index
            layers.loc[d, g] = y.reindex(members).mean()
    return layers


def main() -> None:
    panels, hfq, ret = build_factor_panels()
    fwd = ret.shift(-1)
    rows = []
    layer_stats = {}
    for name, panel in panels.items():
        ic = rank_ic(panel, fwd)
        lr = layer_returns(panel, fwd)
        ls = (lr[5] - lr[1]).dropna()
        years = len(ls) / 252
        stats = {
            "ic_mean": ic.mean(), "icir": ic.mean() / ic.std(),
            "t_stat": ic.mean() / ic.std() * np.sqrt(len(ic)),
            "ls_annual": (1 + ls).prod() ** (252 / len(ls)) - 1,
            "q1_annual": (1 + lr[1].dropna()).prod() ** (252 / len(lr[1].dropna())) - 1,
            "q5_annual": (1 + lr[5].dropna()).prod() ** (252 / len(lr[5].dropna())) - 1,
        }
        rows.append({"factor": name, **stats})
        layer_stats[name] = lr
        print(f"{name}: IC={stats['ic_mean']:.4f} ICIR={stats['icir']:.2f} t={stats['t_stat']:.1f} "
              f"L-S年化={stats['ls_annual']:+.2%} Q1={stats['q1_annual']:+.1%} Q5={stats['q5_annual']:+.1%}")
    df = pd.DataFrame(rows).set_index("factor")
    df.to_csv(ROOT_P / "fund_factor_ic.csv", encoding="utf-8-sig")
    print("\n=== 汇总 ===")
    print(df.round(4).to_string())


if __name__ == "__main__":
    main()
