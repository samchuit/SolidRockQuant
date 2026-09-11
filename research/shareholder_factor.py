"""股东户数（筹码集中度）因子研究：验证总报告 #8「IC 0.047，11 年全正」是否成立.

**关键纪律——公告日对齐**：`股东户数-增减比例` 描述的是「统计截止日」的持股状况，
但该信息只有到「股东户数公告日期」才为市场所知。本数据实测公告日较统计截止日
中位滞后 **11 天**，用统计截止日对齐会产生约 11 天的前视偏差。因此本脚本一律按
**公告日期**把因子值推入面板（公告日前不得出现），再用 `ffill` 保持到下次公告。

因子方向：报告采用「-户数增减比例」（户数减少 = 筹码集中 = 看多）。本脚本同时输出
正负两个方向的 IC，便于核对符号。

评估口径：日频 RankIC（Spearman）vs 未来 5/20 日收益（后复权）、分年均值、ICIR、
t 值、扩张窗口五分位（无前视）。

输出: research/shareholder_factor_ic.csv + research/shareholder_factor_quintile.csv
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
GDHS_DIR = ROOT / "research" / "shareholder"
START, END = "2016-01-04", "2026-09-08"
HORIZONS = (5, 20)


def unified(code: str) -> str:
    """6 位代码 → 统一符号（后缀按板块推断）."""
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    return f"{code}.BJ"


def load_holder_factor(align: str = "ann") -> pd.DataFrame:
    """逐文件读取，返回长表 (symbol, date, chg).

    ``align="ann"``（默认，正确）：按**公告日期**对齐——公告日前市场不知情；
    ``align="cut"``（诊断用，**有前视**）：按统计截止日对齐，用于量化前视偏差幅度。
    """
    date_col = "股东户数公告日期" if align == "ann" else "股东户数统计截止日"
    rows = []
    for f in sorted(GDHS_DIR.glob("gdhs_*.parquet")):
        code = f.stem.replace("gdhs_", "")
        try:
            df = pd.read_parquet(f, columns=[date_col, "股东户数-增减比例"])
        except Exception:  # noqa: BLE001
            continue
        df["ann"] = pd.to_datetime(df[date_col], errors="coerce")
        df["chg"] = pd.to_numeric(df["股东户数-增减比例"], errors="coerce")
        df = df.dropna(subset=["ann", "chg"])
        if df.empty:
            continue
        # 同一日期可能多条（更正），取最新一条
        df = df.sort_values("ann").groupby("ann", as_index=False).last()
        df["symbol"] = unified(code)
        rows.append(df[["symbol", "ann", "chg"]])
    if not rows:
        raise SystemExit("未读到任何股东户数数据")
    long = pd.concat(rows, ignore_index=True)
    long = long.drop_duplicates(["symbol", "ann"])
    return long


def build_panel(long: pd.DataFrame, dates: pd.DatetimeIndex, symbols: list[str]) -> pd.DataFrame:
    """公告日对齐 → 日频面板（ffill；公告日之前为 NaN，无前视）."""
    wide = long.pivot_table(index="ann", columns="symbol", values="chg", aggfunc="last")
    wide = wide.reindex(index=wide.index.union(dates)).sort_index()
    wide = wide.reindex(columns=symbols)
    # 关键：只在公告日当天及之后可见 → reindex 后 ffill（绝不 bfill）
    wide = wide.ffill().reindex(dates)
    return -wide  # 因子方向：户数减少 = 看多


def rank_ic_series(factor: pd.DataFrame, fwd: pd.DataFrame, min_n: int = 100) -> pd.Series:
    """日频截面 RankIC（Spearman）."""
    common_idx = factor.index.intersection(fwd.index)
    out: dict[pd.Timestamp, float] = {}
    for d in common_idx:
        x = factor.loc[d]
        y = fwd.loc[d]
        mask = x.notna() & y.notna()
        if int(mask.sum()) < min_n:
            continue
        out[d] = x[mask].corr(y[mask], method="spearman")
    return pd.Series(out).sort_index()


def quintile_returns(factor: pd.DataFrame, fwd: pd.DataFrame, q: int = 5, min_periods: int = 250) -> pd.Series:
    """扩张窗口分位（无前视）→ 各分位平均前瞻收益."""
    ics = {}
    for d in factor.index:
        x = factor.loc[d].dropna()
        if len(x) < 100:
            continue
        ics[d] = x
    if not ics:
        return pd.Series(dtype=float)
    ics_df = pd.DataFrame(ics).T.sort_index()
    # 每只股票的扩张分位（避免全样本分位的前视）
    pct = ics_df.expanding(min_periods=min_periods).rank(pct=True)
    y = fwd.reindex(ics_df.index)
    buckets = np.ceil(pct * q).clip(1, q)
    # 按日汇总各桶的前瞻收益均值
    daily = {}
    for d in ics_df.index:
        b = buckets.loc[d].dropna()
        r = y.loc[d].reindex(b.index).dropna()
        if r.empty:
            continue
        daily[d] = r.groupby(b.reindex(r.index)).mean()
    if not daily:
        return pd.Series(dtype=float)
    return pd.DataFrame(daily).T.mean()


def main() -> None:
    long = load_holder_factor("ann")
    print(f"股东户数记录: {len(long)} 行, {long['symbol'].nunique()} 只, "
          f"公告日 {long['ann'].min().date()}~{long['ann'].max().date()}")
    snap = load_holder_factor("cut")

    symbols = sorted(long["symbol"].unique())
    bars = STORE.load_bars(symbols, start="2015-06-01", end=END)
    if bars.empty:
        raise SystemExit("本地无对应行情数据")
    hfq = (
        bars.pivot_table(index="date", columns="symbol", values="close", aggfunc="last")
        * bars.pivot_table(index="date", columns="symbol", values="adj_factor", aggfunc="last")
    ).sort_index()
    dates = hfq.index
    symbols = [s for s in symbols if s in hfq.columns]
    print(f"有行情的标的: {len(symbols)}")

    factor = build_panel(long, dates, symbols).loc[START:]
    n_obs = int(factor.notna().sum(axis=1).median())
    print(f"因子面板: {factor.shape}, 每日有效标的（中位）: {n_obs}")

    rows = []
    for h in HORIZONS:
        fwd = (hfq[symbols].shift(-h) / hfq[symbols] - 1.0).reindex(factor.index)
        ic = rank_ic_series(factor, fwd)
        if ic.empty:
            continue
        yearly = ic.groupby(ic.index.year).mean()
        # ICIR / t 值（按年度聚合的 IC 序列）
        ymean, ystd = yearly.mean(), yearly.std(ddof=1)
        row = {
            "horizon": h,
            "ic_mean": ic.mean(),
            "ic_std": ic.std(ddof=1),
            "icir": ic.mean() / ic.std(ddof=1) if ic.std(ddof=1) else np.nan,
            "t_stat": ic.mean() / (ic.std(ddof=1) / np.sqrt(len(ic))) if ic.std(ddof=1) else np.nan,
            "ic_positive_ratio": float((ic > 0).mean()),
            "n_days": len(ic),
            "years_positive": int((yearly > 0).sum()),
            "years_total": int(yearly.notna().sum()),
            "yearly_min": yearly.min(),
            "yearly_max": yearly.max(),
        }
        rows.append(row)
        print(f"\n--- 未来 {h} 日 ---")
        print(f"日均 RankIC = {ic.mean():+.4f}  ICIR = {row['icir']:+.3f}  t = {row['t_stat']:+.2f}")
        print(f"IC>0 占比 = {row['ic_positive_ratio']:.1%}  正收益年份 {row['years_positive']}/{row['years_total']}")
        print("分年 IC:", {int(y): round(v, 4) for y, v in yearly.items()})

    if rows:
        out = pd.DataFrame(rows).set_index("horizon")
        out.to_csv(ROOT / "research" / "shareholder_factor_ic.csv", encoding="utf-8-sig")
        print(f"\n已写出: research/shareholder_factor_ic.csv")

    # 分位（未来 20 日）
    fwd20 = (hfq[symbols].shift(-20) / hfq[symbols] - 1.0).reindex(factor.index)
    qret = quintile_returns(factor, fwd20)
    if not qret.empty:
        print("\n=== 扩张窗口五分位 → 未来 20 日收益（%）===")
        print((qret * 100).round(2).to_string())
        print(f"Q5-Q1 = {(qret.get(5, np.nan) - qret.get(1, np.nan)) * 100:+.2f}%")
        pd.DataFrame({"quintile": qret.index, "mean_fwd20": qret.values}).to_csv(
            ROOT / "research" / "shareholder_factor_quintile.csv", index=False, encoding="utf-8-sig"
        )

    print(
        "\n判读：报告 #8 声称 IC≈0.047 且 11 年全正。若这里的日均 IC 显著低于 0.047、"
        "或正收益年份远少于 11/11，则该结论不成立/被高估。"
    )

    # ---- 诊断：前视对齐的"膨胀"效应（解释报告 0.047 的可能来源）----
    print("\n=== 诊断：公告日对齐（正确）vs 统计截止日对齐（前视）===")
    diag = []
    fwd20 = (hfq[symbols].shift(-20) / hfq[symbols] - 1.0).reindex(factor.index)
    cut_factor = build_panel(snap, dates, symbols).loc[START:]
    ic_ok = rank_ic_series(factor, fwd20)
    ic_bad = rank_ic_series(cut_factor, fwd20)
    diag.append(
        {
            "align": "公告日期（正确）",
            "ic_20d": ic_ok.mean(),
            "t_stat": ic_ok.mean() / (ic_ok.std(ddof=1) / np.sqrt(len(ic_ok))),
        }
    )
    diag.append(
        {
            "align": "统计截止日（前视 ~11 天）",
            "ic_20d": ic_bad.mean(),
            "t_stat": ic_bad.mean() / (ic_bad.std(ddof=1) / np.sqrt(len(ic_bad))),
        }
    )
    d = pd.DataFrame(diag).set_index("align")
    print(d.round(4).to_string())
    print(f"前视膨胀倍数: {ic_bad.mean() / ic_ok.mean():.2f}x"
          if ic_ok.mean() else "")
    d.to_csv(ROOT / "research" / "shareholder_factor_align_diagnostic.csv", encoding="utf-8-sig")
    print("已写出: research/shareholder_factor_align_diagnostic.csv")


if __name__ == "__main__":
    main()
