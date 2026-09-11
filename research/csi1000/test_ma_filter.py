"""补充测试：动量轮动 + 趋势过滤（ma_filter）变体."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from solidrock import BacktestConfig, BacktestEngine, DataStore  # noqa: E402

from dual_momentum import DualMomentum  # noqa: E402
from stress_sweep import WINDOWS, oos_split, run_one  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
K1000_ETF, HS300_ETF, BOND_ETF, GOLD_ETF = "512100.SH", "510300.SH", "511010.SH", "518880.SH"
K1000_IDX, HS300_IDX = "000852.SH", "000300.SH"


def main() -> None:
    store = DataStore(ROOT / ".solidrock")
    etf_start, etf_end, split = WINDOWS["etf"]
    idx_start, idx_end, isplit = WINDOWS["index"]
    cases = [
        ("etf", "MO3a_lb80_rb10_MF200", {"assets": f"{K1000_ETF},{HS300_ETF},{GOLD_ETF}", "defensive": BOND_ETF, "lookback": 80, "rebalance": 10, "ma_filter": 200}, etf_start, etf_end, split),
        ("etf", "MO3a_lb80_rb10_MF100", {"assets": f"{K1000_ETF},{HS300_ETF},{GOLD_ETF}", "defensive": BOND_ETF, "lookback": 80, "rebalance": 10, "ma_filter": 100}, etf_start, etf_end, split),
        ("etf", "MO3a_lb60_rb20_MF200", {"assets": f"{K1000_ETF},{HS300_ETF},{GOLD_ETF}", "defensive": BOND_ETF, "lookback": 60, "rebalance": 20, "ma_filter": 200}, etf_start, etf_end, split),
        ("etf", "MO3a_lb200_rb20_MF200", {"assets": f"{K1000_ETF},{HS300_ETF},{GOLD_ETF}", "defensive": BOND_ETF, "lookback": 200, "rebalance": 20, "ma_filter": 200}, etf_start, etf_end, split),
        ("etf", "MO2a_lb80_rb10_MF200", {"assets": f"{K1000_ETF},{HS300_ETF}", "defensive": BOND_ETF, "lookback": 80, "rebalance": 10, "ma_filter": 200}, etf_start, etf_end, split),
        ("index", "MO3a_lb80_rb10", {"assets": f"{K1000_IDX},{HS300_IDX},{GOLD_ETF}", "defensive": BOND_ETF, "lookback": 80, "rebalance": 10}, idx_start, idx_end, isplit),
        ("index", "MO3a_lb80_rb10_MF200", {"assets": f"{K1000_IDX},{HS300_IDX},{GOLD_ETF}", "defensive": BOND_ETF, "lookback": 80, "rebalance": 10, "ma_filter": 200}, idx_start, idx_end, isplit),
        ("index", "MO3a_lb200_rb20_MF200", {"assets": f"{K1000_IDX},{HS300_IDX},{GOLD_ETF}", "defensive": BOND_ETF, "lookback": 200, "rebalance": 20, "ma_filter": 200}, idx_start, idx_end, isplit),
    ]
    for stage, name, params, start, end, split_year in cases:
        annual, mdd, sharpe, yearly, _ = run_one(DualMomentum, params, start, end, store)
        is_r, oos_r = oos_split(annual, yearly, split_year)
        print(f"[{stage}] {name}: annual={annual:.2%} is={is_r:.2%} oos={oos_r:.2%} mdd={mdd:.1%} "
              f"sharpe={sharpe:.2f} 正收益年={int((yearly > 0).sum())}/{len(yearly)}")
        print("   分年:", {int(k): f"{v:.1%}" for k, v in yearly.items()})


if __name__ == "__main__":
    main()
