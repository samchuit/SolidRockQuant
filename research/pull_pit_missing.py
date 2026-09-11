"""补拉 PIT 宇宙缺失股票的日线+复权因子（Tushare daily/adj_factor，含退市股）.

先聚合 index_weight 快照得到全部历史成分；对比本地库，缺什么拉什么。
Token 从环境变量读取。数据写入 .solidrock 公共库（source=tushare-pit）。
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
ROOT = ENV_FILE.parent
for line in open(ENV_FILE, encoding="utf-8"):
    if line.startswith("SOLIDROCK_TUSHARE_TOKEN="):
        os.environ["SOLIDROCK_TUSHARE_TOKEN"] = line.split("=", 1)[1].strip()

sys_path = str(ROOT)
import sys  # noqa: E402

if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

from solidrock.config import get_settings  # noqa: E402
from solidrock.data.store import DataStore  # noqa: E402

STORE = DataStore(ROOT / ".solidrock")
START, END = "20151201", "20260910"
WORKERS = 3  # tushare 积分限速，保守


def main() -> None:
    import tushare as ts

    pro = ts.pro_api(get_settings().tushare_token)
    w = pd.read_parquet(ROOT / "research" / "tushare" / "index_weight_000852.parquet")
    all_members = sorted(set(w["con_code"]))
    missing = [c for c in all_members if STORE.last_date(c) is None]
    print(f"历史成分合计 {len(all_members)} 只，本地缺失 {len(missing)} 只")

    def fetch_one(code: str) -> str:
        for attempt in range(3):
            try:
                daily = pro.daily(ts_code=code, start_date=START, end_date=END)
                if daily is None or daily.empty:
                    return f"{code}: empty"
                adj = pro.adj_factor(ts_code=code, start_date=START, end_date=END)
                d = daily.rename(columns={"ts_code": "symbol", "trade_date": "date", "vol": "volume",
                                          "amount": "amount"})
                d["date"] = pd.to_datetime(d["date"])
                d = d.sort_values("date").reset_index(drop=True)
                a = adj.rename(columns={"ts_code": "symbol", "trade_date": "date"}) if adj is not None and len(adj) else None
                if a is not None and len(a):
                    a["date"] = pd.to_datetime(a["date"])
                    d = d.merge(a[["date", "adj_factor"]], on="date", how="left")
                else:
                    d["adj_factor"] = float("nan")
                out = pd.DataFrame({
                    "symbol": code, "date": d["date"],
                    "open": d["open"].astype(float), "high": d["high"].astype(float),
                    "low": d["low"].astype(float), "close": d["close"].astype(float),
                    "pre_close": d["pre_close"].astype(float) if "pre_close" in d else d["close"].shift(1),
                    "volume": d["volume"].astype(float), "amount": d["amount"].astype(float),
                    "turnover_rate": float("nan"), "adj_factor": d["adj_factor"], "suspended": False,
                })
                STORE.update_bars(out, source="tushare-pit")
                return f"{code}: ok {len(out)}"
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "每分钟" in msg or "每天" in msg:
                    time.sleep(15)
                elif attempt == 2:
                    return f"{code}: FAIL {type(e).__name__} {msg[:40]}"
                time.sleep(2)
        return f"{code}: retry-exhausted"

    done = 0
    failed = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_one, c): c for c in missing}
        for fut in as_completed(futs):
            r = fut.result()
            done += 1
            if "FAIL" in r or "retry" in r:
                failed.append(r)
            if done % 100 == 0:
                print(f"[{done}/{len(missing)}] failed={len(failed)}")
    print(f"完成，失败 {len(failed)}: {failed[:8]}")


if __name__ == "__main__":
    main()
