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

FEE = 0.0005  # 单边成本（仓位切换时按 |Δpos| 计费）


def quintile_returns(ind: pd.Series, fwd: pd.Series, q: int = 5, min_periods: int = 250) -> pd.Series:
    """按**扩张窗口**分位分组，返回各分位的平均前瞻收益（无前视）.

    分位阈值只用 t 之前的历史（``expanding``），避免"用全样本分位切分历史"
    这一分位研究里最常见的隐性前视。
    """
    idx = ind.dropna().index.intersection(fwd.dropna().index)
    if len(idx) < min_periods:
        return pd.Series(dtype=float)
    x = ind.reindex(idx)
    y = fwd.reindex(idx)
    pct = x.expanding(min_periods=min_periods).apply(lambda w: float((w[:-1] < w[-1]).mean()), raw=True)
    groups = np.ceil(pct * q).clip(1, q)
    return y.groupby(groups).mean()


def timing_backtest(
    signal: pd.Series,
    index_ret: pd.Series,
    *,
    smooth: int | None = None,
    fee: float = FEE,
) -> dict[str, float]:
    """择时：``signal > 0`` 满仓、否则空仓，与买入持有对比.

    - t 日收盘可得信号 → ``shift(1)`` 作用于 t+1 收益（防前视）；
    - ``smooth``：信号先做 n 日均线平滑（降低换手）；
    - 成本：每次仓位变动按 ``|Δpos| × fee`` 扣除。
    """
    sig = signal.rolling(smooth).mean() if smooth else signal
    pos = (sig > 0).astype(float).shift(1).reindex(index_ret.index).fillna(0.0)
    rets = index_ret.reindex(index_ret.index).fillna(0.0)
    net = pos * rets - pos.diff().abs().fillna(0.0) * fee
    nav = (1 + net).cumprod()
    bh = (1 + rets).cumprod()
    years = max((nav.index[-1] - nav.index[0]).days / 365.25, 1e-9)
    return {
        "annual": float(nav.iloc[-1] ** (1 / years) - 1),
        "max_dd": float((nav / nav.cummax() - 1).min()),
        "position_ratio": float(pos.mean()),
        "bh_annual": float(bh.iloc[-1] ** (1 / years) - 1),
        "bh_max_dd": float((bh / bh.cummax() - 1).min()),
        "n_switches": int((pos.diff().abs() > 0).sum()),
    }


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
    print("=== 1) 情绪/资金面指标 RankIC（vs 指数未来收益）===")
    print(res.round(4).to_string())
    print(f"\n已写出: {out}")

    # ---- 2) 分位组合：扩张窗口五分位 → 未来 20 日指数收益（检验单调性）----
    quint_rows = []
    for iname, ind in indicators.items():
        if ind is None or ind.notna().sum() < 400:
            continue
        for label in idx_close.columns:
            qret = quintile_returns(ind, fwd_map[(label, 20)])
            if qret.empty:
                continue
            row: dict[str, object] = {"indicator": iname, "index": label}
            for k, v in qret.items():
                row[f"q{int(k)}"] = v
            row["q5_minus_q1"] = float(qret.get(5, np.nan) - qret.get(1, np.nan))
            quint_rows.append(row)
    quint = pd.DataFrame(quint_rows)
    if not quint.empty:
        quint = quint.set_index(["indicator", "index"])
        qout = ROOT / "research" / "sentiment_quintile.csv"
        quint.to_csv(qout, encoding="utf-8-sig")
        print("\n=== 2) 扩张窗口五分位 → 未来 20 日指数收益（%，检验单调性）===")
        print((quint * 100).round(2).to_string())

    # ---- 3) 择时叠加：信号>0 满仓 / 否则空仓（含 20 日均线平滑版）----
    timing_rows = []
    for iname, ind in indicators.items():
        if ind is None or ind.notna().sum() < 400:
            continue
        for label in idx_close.columns:
            rets = idx_close[label].pct_change()
            for smooth, tag in ((None, "raw"), (20, "ma20")):
                m = timing_backtest(ind, rets, smooth=smooth)
                timing_rows.append(
                    {
                        "indicator": iname,
                        "index": label,
                        "variant": tag,
                        "annual": m["annual"],
                        "max_dd": m["max_dd"],
                        "position_ratio": m["position_ratio"],
                        "bh_annual": m["bh_annual"],
                        "bh_max_dd": m["bh_max_dd"],
                        "excess": m["annual"] - m["bh_annual"],
                        "n_switches": m["n_switches"],
                    }
                )
    timing = pd.DataFrame(timing_rows).set_index(["indicator", "index", "variant"])
    tout = ROOT / "research" / "sentiment_timing.csv"
    timing.to_csv(tout, encoding="utf-8-sig")
    print("\n=== 3) 择时叠加（信号>0 满仓，含 0.05% 单边成本）vs 买入持有（%）===")
    show = timing[["annual", "max_dd", "position_ratio", "bh_annual", "bh_max_dd", "excess"]].copy()
    for c in ("annual", "max_dd", "position_ratio", "bh_annual", "bh_max_dd", "excess"):
        show[c] = (show[c] * 100).round(2)
    print(show.to_string())
    print(f"\n已写出: {tout}")
    print(
        "\n判读：excess = 择时年化 − 买入持有年化。若各指标的最优 excess 不显著为正，"
        "则总报告 #5『情绪/资金面择时有效』不成立（原结论缺产物支撑）。"
    )


if __name__ == "__main__":
    main()
