"""中证1000策略研究：批量回测驱动.

用法（项目根目录）::

    .venv/Scripts/python.exe research/csi1000/run_suite.py --stage etf     # 主窗口（ETF 真实可交易）
    .venv/Scripts/python.exe research/csi1000/run_suite.py --stage index   # 长窗口（指数代理）
    .venv/Scripts/python.exe research/csi1000/run_suite.py --stage stress  # 压力：双倍滑点重跑 ETF 主窗口

输出: research/csi1000/results_<stage>.csv 与分年度明细。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from solidrock import BacktestConfig, BacktestEngine, DataStore  # noqa: E402
from solidrock.backtest.costs import AShareCostModel  # noqa: E402

from dual_ma import DualMASym  # noqa: E402
from dual_momentum import DualMomentum  # noqa: E402
from ma_timing import MaTiming  # noqa: E402
from vol_ma_timing import VolMaTiming  # noqa: E402

_spec = importlib.util.spec_from_file_location("buy_and_hold", ROOT / "examples" / "buy_and_hold.py")
_ba_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ba_mod)
BuyAndHold = _ba_mod.BuyAndHold

ETF_START = "2016-11-04"
ETF_END = "2026-09-08"
IDX_START = "2014-10-17"
IDX_END = "2026-09-09"

K1000_ETF, HS300_ETF, BOND_ETF, GOLD_ETF = "512100.SH", "510300.SH", "511010.SH", "518880.SH"
K1000_IDX, HS300_IDX = "000852.SH", "000300.SH"


def etf_scenarios() -> list[tuple[str, type, dict]]:
    s: list[tuple[str, type, dict]] = [
        ("BH_1000ETF", BuyAndHold, {"symbol": K1000_ETF}),
        ("BH_300ETF", BuyAndHold, {"symbol": HS300_ETF}),
    ]
    for ma in (20, 60, 120, 200):
        s.append((f"MA{ma}_def", MaTiming, {"symbol": K1000_ETF, "ma": ma, "defensive": BOND_ETF}))
    s += [
        ("MA60_cash", MaTiming, {"symbol": K1000_ETF, "ma": 60, "defensive": ""}),
        ("MA200_cash", MaTiming, {"symbol": K1000_ETF, "ma": 200, "defensive": ""}),
    ]
    for fast, slow in ((5, 20), (10, 60), (20, 60)):
        s.append((f"DMA{fast}_{slow}", DualMASym, {"symbol": K1000_ETF, "fast": fast, "slow": slow, "defensive": BOND_ETF}))
    for lb in (20, 60, 120):
        s.append((f"MO2_lb{lb}_rb20", DualMomentum, {"assets": f"{K1000_ETF},{HS300_ETF}", "lookback": lb, "rebalance": 20}))
    s.append(("MO2_lb120_rb10", DualMomentum, {"assets": f"{K1000_ETF},{HS300_ETF}", "lookback": 120, "rebalance": 10}))
    for lb in (60, 120):
        s.append((f"MO3_lb{lb}_rb20", DualMomentum, {"assets": f"{K1000_ETF},{HS300_ETF},{GOLD_ETF}", "lookback": lb, "rebalance": 20}))
    for ma, tv in ((60, 0.20), (60, 0.30), (200, 0.25)):
        s.append((f"VOL_ma{ma}_tv{int(tv*100)}", VolMaTiming, {"symbol": K1000_ETF, "ma": ma, "target_vol": tv, "defensive": BOND_ETF}))
    return s


def index_scenarios() -> list[tuple[str, type, dict]]:
    s = [("BH_1000IDX", BuyAndHold, {"symbol": K1000_IDX})]
    for ma in (60, 200):
        s.append((f"MA{ma}_def", MaTiming, {"symbol": K1000_IDX, "ma": ma, "defensive": BOND_ETF}))
    for lb in (60, 120):
        s.append((f"MO2_lb{lb}_rb20", DualMomentum, {"assets": f"{K1000_IDX},{HS300_IDX}", "lookback": lb, "rebalance": 20}))
    s.append(("VOL_ma200_tv25", VolMaTiming, {"symbol": K1000_IDX, "ma": 200, "target_vol": 0.25, "defensive": BOND_ETF}))
    return s


def yearly_table(nav: pd.DataFrame) -> pd.DataFrame:
    nav = nav.copy()
    if "date" in nav.columns:
        nav["date"] = pd.to_datetime(nav["date"])
        nav = nav.set_index("date")
    else:
        nav.index = pd.to_datetime(nav.index)
    series = nav["total"]
    rows = []
    for year, seg in series.groupby(series.index.year):
        ret = seg.iloc[-1] / seg.iloc[0] - 1.0
        dd = (seg / seg.cummax() - 1.0).min()
        rows.append({"year": year, "ret": ret, "max_dd": dd})
    return pd.DataFrame(rows).set_index("year")


def run_stage(stage: str, slippage_bps: float | None) -> None:
    if stage == "etf":
        scenarios, start, end = etf_scenarios(), ETF_START, ETF_END
    elif stage == "index":
        scenarios, start, end = index_scenarios(), IDX_START, IDX_END
    else:
        scenarios, start, end = etf_scenarios(), ETF_START, ETF_END

    store = DataStore(ROOT / ".solidrock")
    rows, yearlies = [], {}
    for name, cls, params in scenarios:
        cfg = BacktestConfig(
            start=start,
            end=end,
            benchmark=HS300_IDX,
            initial_cash=1_000_000.0,
            max_position_weight=None,  # ETF 无仓位上限约束，基准与策略一致
            name=f"csi1000_{stage}_{name}",
            notes=f"slippage_bps_override={slippage_bps}" if slippage_bps else None,
        )
        if slippage_bps:
            cfg.cost_model = AShareCostModel(slippage_bps=slippage_bps)
        try:
            res = BacktestEngine(cls(**params), cfg, store).run()
        except Exception as exc:  # noqa: BLE001
            rows.append({"scenario": name, "error": str(exc)[:120]})
            continue
        m = res.metrics
        rows.append({
            "scenario": name,
            "total_return": m.get("total_return"),
            "annual_return": m.get("annual_return"),
            "annual_vol": m.get("annual_vol"),
            "sharpe": m.get("sharpe"),
            "max_drawdown": m.get("max_drawdown"),
            "calmar": m.get("calmar"),
            "daily_win_rate": m.get("daily_win_rate"),
            "trade_win_rate": m.get("trade_win_rate"),
            "n_trades": m.get("n_trades") if "n_trades" in m else m.get("trade_count"),
            "annual_turnover": m.get("annual_turnover") if "annual_turnover" in m else m.get("turnover_rate"),
            "total_fees": m.get("total_fees"),
        })
        yt = yearly_table(res.nav)
        yearlies[name] = yt
        print(f"[done] {name}: annual={m.get('annual_return'):.2%} sharpe={m.get('sharpe'):.2f} "
              f"maxdd={m.get('max_drawdown'):.2%} calmar={m.get('calmar'):.2f}")

    df = pd.DataFrame(rows)
    out = Path(__file__).parent / f"results_{stage}.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")
    ypath = Path(__file__).parent / f"yearly_{stage}.csv"
    pd.concat(yearlies, names=["scenario", "year"]).to_csv(ypath, encoding="utf-8-sig")
    cols = ["scenario", "annual_return", "sharpe", "max_drawdown", "calmar"]
    print("\n=== 汇总（按年化排序）===")
    print(df[cols].dropna().sort_values("annual_return", ascending=False).to_string(index=False))
    print(f"\n已写出: {out}\n{ypath}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["etf", "index", "stress"], default="etf")
    ap.add_argument("--slippage", type=float, default=None, help="覆盖滑点 bps（stress 默认 10）")
    args = ap.parse_args()
    slip = args.slippage if args.slippage is not None else (10.0 if args.stage == "stress" else None)
    run_stage(args.stage, slip)
