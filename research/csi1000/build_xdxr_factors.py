"""用 TDX xdxr 除权除息明细重建全成分股复权因子（白盒），并与腾讯 hfq 交叉验证.

- category 1 除权除息: 分红 d=fenhong/10（元/股），送股 s=songzhuangu/10，
  配股 p=peigu/10（价 peigujia）→ 除权参考价 = (pre_close - d + p*peigujia)/(1+s+p)，
  因子比 = pre_close / 除权参考价
- category 11 扩缩股: 因子比 = suogu
- category 5 股本变化等: 无价格调整，跳过
输出:
- fundamentals/xdxr_all.parquet  原始明细
- fundamentals/factor_xdxr.parquet 长表: symbol/date/adj_factor（分段常数）
- 控制台: 与腾讯 hfq 口径的收益差异统计（抽查高分歧股票）
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pytdx.hq import TdxHq_API  # noqa: E402
from solidrock.data.store import DataStore  # noqa: E402

ROOT_P = Path(__file__).parent
OUT = ROOT_P / "fundamentals"
STORE = DataStore(ROOT / ".solidrock")
SERVERS = [("115.238.90.165", 7709), ("218.75.126.9", 7709), ("60.12.136.250", 7709),
           ("180.153.18.170", 7709), ("115.238.90.165", 7715)]


def fetch_xdxr_one(code: str, market: int) -> pd.DataFrame | None:
    for host, port in SERVERS:
        api = TdxHq_API()
        try:
            if not api.connect(host, port, time_out=6):
                continue
            rows = api.get_xdxr_info(market, code)
            api.disconnect()
            if not rows:
                return pd.DataFrame()
            df = api.to_df(rows)
            df["code"] = code
            return df
        except Exception:  # noqa: BLE001
            try:
                api.disconnect()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
    return None


def xdxr_to_factor_events(df: pd.DataFrame) -> pd.DataFrame:
    """xdxr 明细 → (date, factor_ratio) 事件表（只含有价格调整的事件）."""
    ev = []
    for _, r in df.iterrows():
        cat = int(r["category"])
        d = pd.Timestamp(year=int(r["year"]), month=int(r["month"]), day=int(r["day"]))
        try:
            if cat == 11:  # 扩缩股
                ratio = float(r["suogu"]) if pd.notna(r["suogu"]) else None
            elif cat == 1:  # 除权除息
                d_cash = float(r["fenhong"]) / 10.0 if pd.notna(r["fenhong"]) else 0.0
                s = float(r["songzhuangu"]) / 10.0 if pd.notna(r["songzhuangu"]) else 0.0
                p = float(r["peigu"]) / 10.0 if pd.notna(r["peigu"]) else 0.0
                pg = float(r["peigujia"]) if pd.notna(r["peigujia"]) else 0.0
                if d_cash == 0 and s == 0 and p == 0:
                    continue
                ev.append({"date": d, "type": "xdxr", "d": d_cash, "s": s, "p": p, "pg": pg})
                continue
            else:
                continue
        except Exception:  # noqa: BLE001
            continue
        if ratio is not None and 0.05 < ratio < 1.05:
            ev.append({"date": d, "type": "suogu", "ratio": ratio})
    return pd.DataFrame(ev) if ev else pd.DataFrame(columns=["date", "type"])


def main() -> None:
    cons = pd.read_csv(ROOT_P / "cons_current.csv", dtype={"code": str})
    cons["code"] = cons["code"].str.zfill(6)
    market_map = dict(zip(cons["code"], np.where(cons["symbol"].str.endswith(".SH"), 1, 0)))
    codes = cons["code"].tolist()

    all_xdxr: list[pd.DataFrame] = []
    failed: list[str] = []

    def work(code: str):
        return code, fetch_xdxr_one(code, int(market_map[code]))

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(work, c) for c in codes]
        for i, fut in enumerate(as_completed(futs), 1):
            code, df = fut.result()
            if df is None:
                failed.append(code)
            elif len(df):
                all_xdxr.append(df)
            if i % 100 == 0:
                print(f"[{i}/1000] 已失败 {len(failed)}")
    xdxr = pd.concat(all_xdxr, ignore_index=True) if all_xdxr else pd.DataFrame()
    xdxr.to_parquet(OUT / "xdxr_all.parquet", index=False)
    print(f"xdxr 明细: {len(xdxr)} 行, 无记录/失败 {len(failed)} 只: {failed[:10]}")

    # 生成全市场分段常数因子
    store_factors = []
    for sym in cons["symbol"]:
        store_factors.append({"symbol": sym, "date": pd.Timestamp("2015-01-01"), "adj_factor": 1.0})
    # 逐股票事件 → 因子路径（事件日因子比 = pre_close/除权参考价，需 raw pre_close）
    raw_panels = STORE.load_bars(cons["symbol"].tolist(), start="2014-12-01", end="2026-09-09")
    pre_close = raw_panels.pivot_table(index="date", columns="symbol", values="pre_close", aggfunc="last")
    for code, g in xdxr.groupby("code"):
        sym = f"{code}.SH" if f"{code}.SH" in pre_close.columns else f"{code}.SZ"
        if sym not in pre_close.columns:
            continue
        ev = xdxr_to_factor_events(g)
        for _, e in ev.iterrows():
            d = e["date"]
            if d not in pre_close.index:
                continue
            pc = pre_close.loc[d, sym]
            if pd.isna(pc) or pc <= 0:
                continue
            if e["type"] == "suogu":
                ratio = float(e["ratio"])
            else:
                ref_price = (pc - e["d"] + e["p"] * e["pg"]) / (1 + e["s"] + e["p"])
                ratio = pc / ref_price
            # 送股/转增事件的因子比合法地 >1（10送10 → 2.0），上限放宽到 20；
            # 与实际价格对照防脏数据：除权参考价与 pre_close 同量级
            if not (0.05 < ratio < 20.0) or not np.isfinite(ratio):
                continue
            if pc <= 0 or ref_price <= 0:
                continue
            store_factors.append({"symbol": sym, "date": d, "adj_factor": ratio})
    events = pd.DataFrame(store_factors)
    events.to_parquet(OUT / "factor_events_xdxr.parquet", index=False)
    print(f"因子事件: {len(events)} 条（含每股票起始 1.0）")

    # 交叉验证：xdxr 分段因子 vs 腾讯 hfq/raw 因子，比日收益差
    bars = STORE.load_bars(cons["symbol"].tolist(), start="2015-01-01", end="2026-09-09").sort_values(
        ["symbol", "date"])
    tx_factor = bars.set_index(["symbol", "date"])["adj_factor"]
    ev_idx = events.set_index(["symbol", "date"])["adj_factor"]
    sample_diffs = []
    for sym in list(cons["symbol"].sample(12, random_state=7)):
        try:
            evf = ev_idx.loc[sym].sort_index().groupby(level=0).last().cumprod()
            txf = tx_factor.loc[sym].sort_index()
            common = evf.index.intersection(txf.dropna().index)
            if len(common) < 100:
                continue
            ev_adj = evf.reindex(common).ffill()
            ev_adj = ev_adj / ev_adj.iloc[-1]
            tx_adj = txf.reindex(common).dropna()
            tx_adj = tx_adj / tx_adj.iloc[-1]
            diff = (ev_adj / tx_adj.reindex(ev_adj.index) - 1).abs().max()
            sample_diffs.append({"symbol": sym, "max_factor_gap": diff})
        except Exception as e:  # noqa: BLE001
            sample_diffs.append({"symbol": sym, "max_factor_gap": np.nan, "err": str(e)[:40]})
    print("\n=== xdxr白盒 vs 腾讯黑盒 因子一致性抽查（相对最新点归一）===")
    print(pd.DataFrame(sample_diffs).round(5).to_string(index=False))


if __name__ == "__main__":
    main()
