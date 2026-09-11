"""ETF 网格策略实证（512100 中证1000ETF，日频模拟）.

机制：乘法网格（锚定价 A，格距 s，格线 A×(1±s)^k）；价格下穿格线买 1 股，
上穿卖 1 股；现金初值 = U_max × 初始价（即满仓等值资金），仓位上限 U_max 股。
锚定价每 60 日按滚动中位数重定（格网随中枢漂移）。每笔单边费用 FEE。

**复权口径（关键）**：网格在**后复权价**上运行。用原始价会踩到份额合并/拆分
造成的假跳变（如 512100 在 2022-09-05 份额合并，原始价 0.982 → 2.713），
网格会把这种非经济性的价格跳变当作连续行情在虚高价卖出，结论被显著高估。
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
START, END = "2017-01-03", "2026-09-08"
FEE = 0.0015
ANCHOR_WIN = 60


def run_grid(close: pd.Series, step: float, u_max: int) -> pd.Series:
    px = close.to_numpy()
    dates = close.index
    cash = u_max * px[0]  # 满仓等值现金
    units = 0
    anchor = float(px[0])
    levels = anchor * (1 + step) ** np.arange(-25, 26)
    last_idx: int | None = None
    nav = np.zeros(len(px))
    for i, p in enumerate(px):
        if i >= ANCHOR_WIN and i % ANCHOR_WIN == 0:
            anchor = float(np.median(px[i - ANCHOR_WIN:i]))
            levels = anchor * (1 + step) ** np.arange(-25, 26)
            last_idx = None
        idx_now = int(np.argmin(np.abs(np.log(levels / p))))
        if last_idx is None:
            last_idx = idx_now
        moves = idx_now - last_idx
        for _ in range(abs(moves)):
            if moves < 0 and cash >= p * (1 + FEE) and units < u_max:
                cash -= p * (1 + FEE)
                units += 1
            elif moves > 0 and units > 0:
                cash += p * (1 - FEE)
                units -= 1
        last_idx = idx_now
        nav[i] = cash + units * p
    return pd.Series(nav / (u_max * px[0]), index=dates)


def max_dd(nav: pd.Series) -> float:
    return float((nav / nav.cummax() - 1).min())


def main() -> None:
    # 后复权价：跨份额合并/分红连续，避免假跳变污染网格成交
    close = STORE.load_bars("512100.SH", start=START, end=END, adjust="hfq").set_index("date")["close"]
    years = (close.index[-1] - close.index[0]).days / 365.25
    bh_annual = (close.iloc[-1] / close.iloc[0]) ** (1 / years) - 1
    print(f"买入持有 512100（后复权）: 年化={bh_annual:+.2%} 最大回撤={max_dd(close):.1%}")

    rows = []
    for step in (0.03, 0.05, 0.08, 0.12):
        for u_max in (10, 20):
            nav = run_grid(close, step, u_max)
            ann = nav.iloc[-1] ** (1 / years) - 1
            dd = max_dd(nav)
            rows.append({"step": step, "u_max": u_max, "annual": ann, "max_dd": dd, "final_nav": nav.iloc[-1]})
            print(f"网格 step={step:.0%} U={u_max}: 年化={ann:+.2%} 回撤={dd:.1%}")
    pd.DataFrame(rows).to_csv(Path(__file__).parent / "grid_results.csv", index=False, encoding="utf-8-sig")
    print(f"\n已写出 grid_results.csv（对照买入持有年化 {bh_annual:+.2%}）")


if __name__ == "__main__":
    main()
