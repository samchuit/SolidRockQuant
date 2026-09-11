"""压力测试：参数敏感性扫描 + 样本内外切分.

对入围策略族做参数网格扫描，检验性能是否落在"平台"而非孤峰：
- MaTiming: ma ∈ {20,60,100,140,180,220} × 防守资产 {国债ETF, 空仓} × 标的 {512100, 000852}
- DualMomentum: 资产池 {1000+300, 1000+300+黄金} × lookback {20,40,60,80,120,160,200} × rebalance {10,20}

每条记录附样本内（前半段）与样本外（后半段）年化，检验前视稳健性：
- ETF 窗口: IS=2017-02~2021-12，OOS=2022-01~2026-09
- 指数窗口: IS=2015-01~2020-12，OOS=2021-01~2026-09
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from solidrock import BacktestConfig, BacktestEngine, DataStore  # noqa: E402

from dual_momentum import DualMomentum  # noqa: E402
from ma_timing import MaTiming  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
K1000_ETF, HS300_ETF, BOND_ETF, GOLD_ETF = "512100.SH", "510300.SH", "511010.SH", "518880.SH"
K1000_IDX, HS300_IDX = "000852.SH", "000300.SH"

WINDOWS = {
    "etf": ("2017-02-06", "2026-09-08", 2021),   # 起点延后使 lookback≤240 的指标就绪
    "index": ("2015-10-26", "2026-09-09", 2020),  # 同理（000852 数据 2014-10 起）
}


def run_one(cls, params: dict, start: str, end: str, store: DataStore):
    cfg = BacktestConfig(
        start=start, end=end, benchmark=HS300_IDX,
        max_position_weight=None, name=f"sweep_{cls.__name__}", log_experiment=False,
    )
    res = BacktestEngine(cls(**params), cfg, store).run()
    nav = res.nav.copy()
    if "date" in nav.columns:
        nav = nav.set_index(pd.to_datetime(nav["date"]))
    else:
        nav.index = pd.to_datetime(nav.index)
    series = nav["total"]
    years = (series.index[-1] - series.index[0]).days / 365.25
    total = series.iloc[-1] / series.iloc[0] - 1.0
    annual = (1 + total) ** (1 / years) - 1
    mdd = res.metrics["max_drawdown"]
    sharpe = res.metrics["sharpe"]
    ret = series.pct_change().dropna()
    yearly = (1 + ret).groupby(ret.index.year).prod() - 1
    return annual, mdd, sharpe, yearly, res.metrics


def oos_split(annual: float, yearly: pd.Series, split_year: int):
    """按年收益序列切样本内/样本外年化（近似，用几何平均）."""
    def geo(rets: pd.Series) -> float:
        if len(rets) == 0:
            return float("nan")
        return (1 + rets).prod() ** (1 / len(rets)) - 1
    return geo(yearly[yearly.index <= split_year]), geo(yearly[yearly.index > split_year])


def main() -> None:
    store = DataStore(ROOT / ".solidrock")
    rows: list[dict] = []

    def add_row(stage, family, params, start, end, split_year, store=store):
        annual, mdd, sharpe, yearly, metrics = run_one(cls, params, start, end, store)
        is_r, oos_r = oos_split(annual, yearly, split_year)
        rows.append({
            "stage": stage, "family": family, **{k: v for k, v in params.items()},
            "annual": annual, "sharpe": sharpe, "max_dd": mdd,
            "is_annual": is_r, "oos_annual": oos_r,
            "pos_years": int((yearly > 0).sum()), "n_years": len(yearly),
            "yearly": yearly.to_dict(),
        })
        print(f"[{stage}] {family} {params}: annual={annual:.2%} is={is_r:.2%} oos={oos_r:.2%} "
              f"mdd={mdd:.1%} sharpe={sharpe:.2f} 正收益年={rows[-1]['pos_years']}/{len(yearly)}")

    for stage, (start, end, split_year) in WINDOWS.items():
        sym = K1000_ETF if stage == "etf" else K1000_IDX
        for ma in (20, 60, 100, 140, 180, 220):
            for dname, dval in (("bond", BOND_ETF), ("cash", "")):
                cls, params = MaTiming, {"symbol": sym, "ma": ma, "defensive": dval}
                add_row(stage, f"MA{ma}_{dname}", params, start, end, split_year)

    etf_start, etf_end, split = WINDOWS["etf"]
    for assets_name, assets in (("2a", f"{K1000_ETF},{HS300_ETF}"), ("3a", f"{K1000_ETF},{HS300_ETF},{GOLD_ETF}")):
        for lb in (20, 40, 60, 80, 120, 160, 200):
            for rb in (10, 20):
                cls, params = DualMomentum, {"assets": assets, "defensive": BOND_ETF, "lookback": lb, "rebalance": rb}
                add_row("etf", f"MO{assets_name}_lb{lb}_rb{rb}", params, etf_start, etf_end, split)

    df = pd.DataFrame(rows)
    out = Path(__file__).parent / "stress_sweep.csv"
    df.drop(columns=["yearly"]).to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n已写出: {out}（共 {len(df)} 条）")


if __name__ == "__main__":
    main()
