"""拉取基本面数据：① 东财业绩报表（2015Q1~2026Q2，含公告日期）；② 百度总市值历史.

输出:
- research/csi1000/fundamentals/yjbb.parquet  长表: 股票代码/报告期/公告日期/各财务指标
- research/csi1000/fundamentals/mktcap.parquet 长表: 股票代码/日期/总市值
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

OUT_DIR = Path(__file__).parent / "fundamentals"
OUT_DIR.mkdir(exist_ok=True)
CONS_FILE = Path(__file__).parent / "cons_current.csv"
WORKERS = 6
_lock = threading.Lock()
_done = 0
_failed: list[str] = []


def fetch_yjbb() -> pd.DataFrame:
    periods = pd.period_range("2015Q1", "2026Q2", freq="Q")
    frames = []
    for p in periods:
        d = p.end_time.strftime("%Y%m%d")
        for attempt in range(3):
            try:
                df = ak.stock_yjbb_em(date=d)
                if df is None or df.empty:
                    raise ValueError("empty")
                df["报告期"] = p.end_time.normalize()
                frames.append(df)
                print(f"yjbb {d}: {len(df)} 行")
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    print(f"yjbb {d} FAILED: {type(e).__name__} {str(e)[:60]}")
                time.sleep(2)
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    out.to_parquet(OUT_DIR / "yjbb.parquet", index=False)
    print(f"yjbb 合计: {len(out)} 行 → {OUT_DIR / 'yjbb.parquet'}")
    return out


def _fetch_mktcap_one(code: str) -> pd.DataFrame | None:
    df = ak.stock_zh_valuation_baidu(symbol=code, indicator="总市值", period="全部")
    if df is None or df.empty:
        raise ValueError("empty")
    df = df.rename(columns={"date": "date", "value": "mktcap"})
    df["date"] = pd.to_datetime(df["date"])
    df["code"] = code
    return df[["code", "date", "mktcap"]]


def fetch_mktcap() -> None:
    global _done
    cons = pd.read_csv(CONS_FILE, dtype={"code": str})
    cons["code"] = cons["code"].str.zfill(6)
    log = OUT_DIR / "mktcap_log.txt"
    todo = [c for c in cons["code"] if not (OUT_DIR / f"mktcap_{c}.parquet").exists()]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_fetch_mktcap_one, c): c for c in todo}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                df = fut.result()
                df.to_parquet(OUT_DIR / f"mktcap_{c}.parquet", index=False)
                status = f"ok {len(df)}"
            except Exception as e:  # noqa: BLE001
                status = f"FAIL {type(e).__name__}: {str(e)[:50]}"
                _failed.append(c)
            _done += 1
            if _done % 50 == 0 or status.startswith("FAIL"):
                with _lock:
                    with open(log, "a", encoding="utf-8") as f:
                        f.write(f"[{_done}/{len(todo)}] {c} -> {status}\n")
    with _lock:
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"=== 完成，失败 {len(_failed)}: {_failed[:20]} ===\n")
    print(f"mktcap 完成，失败 {len(_failed)} 只")


if __name__ == "__main__":
    if sys.argv[1:] and sys.argv[1] == "mktcap":
        fetch_mktcap()
    else:
        fetch_yjbb()
