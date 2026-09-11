"""用腾讯后复权数据重建 ETF 复权因子（权威修复，幂等）.

诊断结论（详见研究笔记）：
- 东财/腾讯/新浪三源的 512100 原始价完全一致，000852 指数两源一致 → 原始数据可信；
- 512100 唯一的大额公司行为是 2022-09-05 份额合并（约 1:2.7474）；
- 2019-2020 年 ETF 相对价格指数的超额收益为流动性稀薄期的真实溢价扩张（平滑无离散跳变），
  另有小额现金分红（腾讯 hfq 隐含因子 0.3748 vs 纯合并因子 0.3647，差 +2.7%）。

方法：``adj_factor = 腾讯后复权收盘 / 本地原始收盘``（按日对齐），因子应为分段常数，
跳变仅出现在公司行为日。写库前打印全部跳变日供核对。
"""

from __future__ import annotations

from pathlib import Path

import akshare as ak
import pandas as pd

from solidrock.data.store import DataStore

ROOT = Path(__file__).resolve().parents[2]
# 仅 512100.SH 用腾讯 hfq 重建因子（关键修复：2022-09-05 份额合并）；
# 510300 的腾讯 hfq 含 60+ 个 ±0.5~1% 伪跳变（该 ETF 高流动性，不可能有这么多
# 公司行为，应为净值口径伪影），故 510300/511010/518880 因子置 1：
# 代价是 510300 分红（约 1%/年）不计入，方向上对策略结论保守。
REBUILD_FROM_TX = {"512100.SH"}
FACTOR_ONE = ["510300.SH", "511010.SH", "518880.SH"]


def main() -> None:
    store = DataStore(ROOT / ".solidrock")
    for sym in REBUILD_FROM_TX | set(FACTOR_ONE):
        raw = store.load_bars(sym).sort_values("date").reset_index(drop=True)
        raw_close = raw["close"].astype(float)
        if sym in REBUILD_FROM_TX:
            code = sym.split(".")[0]
            hfq = ak.stock_zh_a_hist_tx(symbol=f"sh{code}", start_date="20120101", end_date="20260909", adjust="hfq")
            hfq["date"] = pd.to_datetime(hfq["date"])
            hfq_close = hfq.set_index("date")["close"].astype(float)
            factor_raw = raw["date"].map(hfq_close) / raw_close
            # 清洗：腾讯 hfq 3 位小数舍入带来 ±3bp 逐日抖动；仅保留 >0.5% 的因子跳变
            # （公司行为），其余日子因子维持前值（分段常数）。
            cleaned: list[float] = []
            prev: float | None = None
            for f in factor_raw:
                if pd.isna(f):
                    cleaned.append(prev if prev is not None else float("nan"))
                    continue
                if prev is not None and abs(f / prev - 1.0) <= 0.005:
                    cleaned.append(prev)
                else:
                    prev = float(f)
                    cleaned.append(prev)
            factor = pd.Series(cleaned, index=raw.index)
        else:
            factor = pd.Series(1.0, index=raw.index)
        raw["adj_factor"] = factor

        # 因子跳变日（应仅为公司行为日）
        ratio = factor / factor.shift(1)
        jumps = raw.loc[(ratio - 1).abs() > 1e-6, ["date", "close"]].assign(factor_ratio=ratio)
        print(f"\n=== {sym}: 因子跳变日（公司行为）===")
        print(jumps.to_string(index=False) if len(jumps) else "（无）")

        store.save_bars(raw, source="adj-fix-final")

        # 年度收益验证：修复后 ETF vs 指数
        ref = {"512100.SH": "000852.SH", "510300.SH": "000300.SH"}.get(sym)
        if ref:
            idx = store.load_bars(ref).set_index("date")["close"]
            common = raw["date"].pipe(pd.DatetimeIndex)
            adj_close = (raw_close * factor).set_axis(common)
            er = adj_close.reindex(idx.index).pct_change()
            ir = idx.pct_change()
            df = pd.DataFrame({"etf": er, "idx": ir}).dropna()
            y = ((1 + df).groupby(df.index.year).prod() - 1)
            y["gap"] = y["etf"] - y["idx"]
            print(y.round(4).to_string())
    print("\n修复完成。")


if __name__ == "__main__":
    main()
