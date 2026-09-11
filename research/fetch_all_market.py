"""全市场 A 股日线批量拉取（TDX 源，多线程断点续传）→ .solidrock 公共库.

用途：情绪/资金面择时指标的全市场统计（涨停家数、赚钱效应、新高数等）。
从 2016-01-01 起（与主研究窗口一致）。已覆盖到末日的股票自动跳过。
"""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from solidrock.data.sources import create_source  # noqa: E402
from solidrock.data.store import DataStore  # noqa: E402

CONS = ROOT / "research" / "all_market_stocks.csv"
LOG = ROOT / "research" / "fetch_all_log.txt"
START, END = "2016-01-01", "2026-09-10"
WORKERS = 6

_lock = threading.Lock()
_done = _ok = _skip = 0
_failed: list[str] = []


def log(msg: str) -> None:
    with _lock:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


def fetch_one(symbol: str, store: DataStore, src) -> str:
    last = store.last_date(symbol)
    if last is not None and last >= pd.Timestamp("2026-09-10"):
        return "skip"
    for attempt in range(3):  # 空结果重试（服务器限流/抖动）
        df = src.fetch_bars([symbol], start=START, end=END, freq="1d")
        if df is not None and not df.empty:
            store.update_bars(df, source="tdx-bulk-all")
            return f"ok {len(df)}"
        time.sleep(0.5 * (attempt + 1))
    return "empty"


def main() -> None:
    global _done, _ok, _skip
    store = DataStore(ROOT / ".solidrock")
    # 每线程绑定不同服务器，分散单点负载
    import json
    import threading
    sf = ROOT / ".solidrock" / "tdx_servers.json"
    pool = [tuple(s) for s in json.loads(sf.read_text(encoding="utf-8"))["servers"]] if sf.exists() else []
    local = threading.local()
    all_syms = pd.read_csv(CONS, dtype={"symbol": str})["symbol"].tolist()
    todo = [s for s in all_syms if not (store.last_date(s) and store.last_date(s) >= pd.Timestamp("2026-09-10"))]
    srcs = {i: create_source("tdx", servers=[pool[i % len(pool)]] if pool else None) for i in range(WORKERS)}
    src_holder = {"srcs": srcs}
    symbols = pd.read_csv(CONS, dtype={"symbol": str})["symbol"].tolist()
    log(f"=== 启动 {len(symbols)} 只，{START}~{END}，WORKERS={WORKERS} ===")
    def fetch_with_src(sym):
        tid = threading.get_ident()
        if not hasattr(local, "n"):
            local.n = len(todo) and _done % WORKERS
        return fetch_one(sym, store, src_holder["srcs"][hash(tid) % WORKERS])

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_with_src, s): s for s in todo}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                status = fut.result()
            except Exception as e:  # noqa: BLE001
                status = f"FAIL {type(e).__name__}: {str(e)[:50]}"
                _failed.append(s)
            _done += 1
            if status.startswith("ok"):
                _ok += 1
            elif status == "skip":
                _skip += 1
            if _done % 100 == 0 or status.startswith("FAIL"):
                log(f"[{_done}/{len(symbols)}] {s} -> {status} (ok={_ok} skip={_skip} fail={len(_failed)})")
    log(f"=== 完成 ok={_ok} skip={_skip} 失败 {len(_failed)}: {_failed[:20]} ===")
    print(f"完成 ok={_ok} skip={_skip} fail={len(_failed)}")


if __name__ == "__main__":
    main()
