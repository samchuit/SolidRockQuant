"""批量拉取中证1000成分股日线（腾讯主源，raw+hfq → adj_factor），断点续传.

背景：东财 push2his 经代理不稳定、新浪股票接口失效，腾讯 stock_zh_a_hist_tx
当日验证稳定（且含当日数据）。换手率字段腾讯不提供，置 NaN（因子改用 Amihud）。

- adj_factor = hfq_close / raw_close（不清洗，保证 close×factor 与 hfq 完全一致，
  因子研究的收益序列由此精确连续；事件引擎如需分段常数因子另行清洗）；
- pre_close 用 close.shift(1) 近似（除权日不修正；因子研究不用该列）；
- 入库 store.update_bars；已覆盖到末日的股票跳过；日志 fetch_log.txt。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

os.environ.setdefault("TQDM_DISABLE", "1")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import akshare as ak  # noqa: E402
import pandas as pd  # noqa: E402

from solidrock.data.store import DataStore  # noqa: E402

START = "2015-01-01"
END = "20260909"
WORKERS = 6
CONS_FILE = Path(__file__).parent / "cons_current.csv"
LOG_FILE = Path(__file__).parent / "fetch_log.txt"

_lock = threading.Lock()
_done = 0
_failed: list[str] = []


def log(msg: str) -> None:
    with _lock:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


def _tx(code: str, adjust: str) -> pd.DataFrame:
    sym = ("sh" if code.startswith("6") else "sz") + code
    df = ak.stock_zh_a_hist_tx(symbol=sym, start_date=START.replace("-", ""), end_date=END, adjust=adjust)
    if df is None or df.empty:
        raise ValueError("empty")
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def fetch_one(symbol: str, store: DataStore) -> str:
    last = store.last_date(symbol)
    if last is not None and last >= pd.Timestamp("2026-09-09"):
        return "skip"
    code = symbol.split(".")[0]
    raw, hfq = _tx(code, ""), _tx(code, "hfq")
    hfq_c = hfq.set_index("date")["close"].astype(float)
    close = raw["close"].astype(float)
    out = pd.DataFrame({
        "symbol": symbol,
        "date": raw["date"],
        "open": raw["open"].astype(float),
        "high": raw["high"].astype(float),
        "low": raw["low"].astype(float),
        "close": close,
        "pre_close": close.shift(1),
        "volume": raw["volume"].astype(float),
        "amount": raw["amount"].astype(float),
        "turnover_rate": float("nan"),
        "adj_factor": raw["date"].map(hfq_c) / close,
        "suspended": False,
    })
    store.update_bars(out, source="tx-bulk-csi1000")
    return f"ok {len(out)}"


def main() -> None:
    global _done
    store = DataStore(ROOT / ".solidrock")
    symbols = pd.read_csv(CONS_FILE)["symbol"].tolist()
    log(f"=== 启动 {len(symbols)} 只，START={START} END={END} WORKERS={WORKERS} ===")
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_one, s, store): s for s in symbols}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                status = fut.result()
            except Exception as e:  # noqa: BLE001
                status = f"FAIL {type(e).__name__}: {str(e)[:60]}"
                _failed.append(s)
            _done += 1
            if _done % 25 == 0 or status.startswith("FAIL"):
                log(f"[{_done}/{len(symbols)}] {s} -> {status}")
    log(f"=== 完成，失败 {len(_failed)} 只: {_failed[:30]} ===")
    print(f"完成，失败 {len(_failed)} 只: {_failed[:10]}")


if __name__ == "__main__":
    main()
