"""Top-N 选股组合回测（向量化，含成本与基准对比）.

- 打分：给定因子名列表，各因子截面 rank(pct) 后等权合成（rank 均值，越高越好）；
- 组合：每个再平衡日取打分最高前 N 只等权持有至下次再平衡；
- 执行：vectorized_backtest（权重 t 日收盘决定、t+1 生效，内置 lag=1 防前视）；
- 基准：000852 中证1000 价格指数（不含股息，对策略超额的估计偏保守 ~1%/年）；
- 注意：宇宙为当前成分，存在幸存者偏差，多头绝对收益仅供参考，重点看相对结构与参数稳健性。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from solidrock.backtest.vectorized import vectorized_backtest  # noqa: E402
from solidrock.data.store import DataStore  # noqa: E402
from solidrock.factors.base import FactorData  # noqa: E402

STORE = DataStore(ROOT / ".solidrock")
CONS_FILE = Path(__file__).parent / "cons_current.csv"
START, END = "2016-01-04", "2026-09-08"


def load_panels(universe: list[str] | None = None) -> FactorData:
    """``universe=None`` 时用当前成分（含幸存者偏差）；传入 PIT 宇宙可消除该偏差."""
    if universe is None:
        universe = pd.read_csv(CONS_FILE)["symbol"].tolist()
    bars = STORE.load_bars(universe, start="2015-01-01", end=END)
    return FactorData.from_bars(bars)


def factor_panel(name: str, data: FactorData) -> pd.DataFrame:
    hfq = data.hfq_close()
    ret = hfq.pct_change()
    if name == "MOM20":
        return hfq / hfq.shift(20) - 1.0
    if name == "MOM60":
        return hfq / hfq.shift(60) - 1.0
    if name == "MOM120":
        return hfq / hfq.shift(120) - 1.0
    if name == "REV5":
        return -(hfq / hfq.shift(5) - 1.0)
    if name == "REV20":
        return -(hfq / hfq.shift(20) - 1.0)
    if name == "VOL20":
        return -ret.rolling(20).std()
    if name == "AMP20":
        return -((data.high / data.low - 1.0).rolling(20).mean())
    if name == "ILLIQ20":
        return (ret.abs() / data.amount).rolling(20).mean() * 1e9
    if name == "TURNOVER20":
        return -data.turnover_rate.rolling(20).mean()  # 数据可得时用
    raise ValueError(f"未知因子 {name}")


def build_weights(scores: pd.DataFrame, top_n: int, rebal: str | int) -> pd.DataFrame:
    """再平衡日生成等权目标权重（未入选=0），其余日期保持上一目标."""
    if rebal == "monthly":
        keys = scores.index.to_series().groupby(scores.index.to_period("M")).min()
        rebal_days = pd.DatetimeIndex(keys.values)
    elif rebal == "weekly":
        keys = scores.index.to_series().groupby(scores.index.to_period("W")).min()
        rebal_days = pd.DatetimeIndex(keys.values)
    else:
        step = int(rebal)
        rebal_days = scores.index[::step]
    weights = pd.DataFrame(np.nan, index=scores.index, columns=scores.columns)
    for day in rebal_days:
        if day not in scores.index:
            continue
        row = scores.loc[day].dropna()
        if len(row) < top_n:
            continue
        weights.loc[day, :] = 0.0  # 全体先清零（显式目标），再写入入选者
        picks = row.nlargest(top_n).index
        weights.loc[day, picks] = 1.0 / top_n
    weights = weights.ffill().fillna(0.0)
    return weights


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--factors", default="REV5,VOL20")
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--rebal", default="monthly", help="monthly / weekly / 数字(交易日)")
    ap.add_argument("--fee", type=float, default=0.0015, help="单边费率")
    ap.add_argument("--tag", default="run")
    args = ap.parse_args()

    data = load_panels()
    names = [s.strip() for s in args.factors.split(",") if s.strip()]
    rank_sum, rank_cnt = None, 0
    for n in names:
        if n == "TURNOVER20" and data.turnover_rate is None or (n == "TURNOVER20" and data.turnover_rate.notna().sum().sum() == 0):
            print(f"[skip] {n}: 无换手率数据")
            continue
        r = factor_panel(n, data).rank(axis=1, pct=True)
        rank_sum = r if rank_sum is None else rank_sum.add(r, fill_value=None)
        rank_cnt += 1
    scores = rank_sum / rank_cnt

    weights = build_weights(scores, args.top, args.rebal)
    res = vectorized_backtest(weights, data.hfq_close(), fee_rate=args.fee)
    m = res.metrics

    # 基准：000852 价格指数
    idx = STORE.load_bars("000852.SH", start=START, end=END).set_index("date")["close"]
    bench_ret = idx.pct_change().reindex(res.port_returns.index).fillna(0.0)
    bench_annual = (1 + bench_ret).prod() ** (252 / len(bench_ret)) - 1

    years = (res.nav.index[-1] - res.nav.index[0]).days / 365.25
    ret_y = res.port_returns
    yearly = (1 + ret_y).groupby(ret_y.index.year).prod() - 1
    b_yearly = (1 + bench_ret).groupby(bench_ret.index.year).prod() - 1

    print(f"[{args.tag}] factors={args.factors} top={args.top} rebal={args.rebal} fee={args.fee:.2%}")
    print(f"  年化={m['annual_return']:.2%} 波动={m.get('annual_vol', float('nan')):.2%} "
          f"夏普={m.get('sharpe', float('nan')):.2f} 回撤={m['max_drawdown']:.2%} "
          f"换手={res.turnover.mean() * 252:.1f}x/年")
    print(f"  基准年化={bench_annual:.2%} 超额年化≈{m['annual_return'] - bench_annual:+.2%}")
    print("  分年（策略/基准/超额）:")
    for y in yearly.index:
        print(f"    {y}: {yearly[y]:+.1%} / {b_yearly.get(y, float('nan')):+.1%} / {yearly[y] - b_yearly.get(y, 0.0):+.1%}")

    # 追加写 CSV
    out = Path(__file__).parent / "portfolio_results.csv"
    row = {"tag": args.tag, "factors": args.factors, "top": args.top, "rebal": args.rebal,
           "fee": args.fee, "annual": m["annual_return"], "sharpe": m.get("sharpe"),
           "max_dd": m["max_drawdown"], "bench_annual": bench_annual,
           "excess": m["annual_return"] - bench_annual,
           "turnover_yr": res.turnover.mean() * 252}
    pd.DataFrame([row]).to_csv(out, mode="a", header=not out.exists(), index=False, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
