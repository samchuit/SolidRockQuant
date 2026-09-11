"""解析通达信 vipdoc 日线二进制文件（.day）→ 标准 DataFrame.

格式（每记录 32 字节，小端）:
  [0:4]  int32  日期 YYYYMMDD
  [4:8]  int32  开盘 ×100
  [8:12] int32  最高 ×100
  [12:16] int32 最低 ×100
  [16:20] int32 收盘 ×100
  [20:24] float32 成交额（元）
  [24:28] int32 成交量（股，股票口径）
  [28:32] int32 保留（部分版本为昨收 ×100）

用法:
  python tdx_parse.py <vipdoc根目录> <输出目录>
  解析 vipdoc/{sh,sz}/lday/*.day 中属于中证1000 成分 + 主要指数的文件。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np
import pandas as pd

RECORD = struct.Struct("<IIIIIfII")


def parse_day_file(path: Path) -> pd.DataFrame:
    raw = path.read_bytes()
    n = len(raw) // 32
    if n == 0:
        return pd.DataFrame()
    recs = np.frombuffer(raw[: n * 32], dtype=np.uint8).reshape(n, 32)
    dates = np.zeros(n, dtype=np.int64)
    opens = np.zeros(n, dtype=np.int64)
    highs = np.zeros(n, dtype=np.int64)
    lows = np.zeros(n, dtype=np.int64)
    closes = np.zeros(n, dtype=np.int64)
    amounts = np.zeros(n, dtype=np.float64)
    vols = np.zeros(n, dtype=np.int64)
    for i in range(n):
        d, o, h, l, c, a, v, _ = RECORD.unpack(recs[i].tobytes())
        dates[i], opens[i], highs[i], lows[i], closes[i] = d, o, h, l, c
        amounts[i], vols[i] = a, v
    df = pd.DataFrame({
        "date": pd.to_datetime(dates.astype(str), format="%Y%m%d", errors="coerce"),
        "open": opens / 100.0,
        "high": highs / 100.0,
        "low": lows / 100.0,
        "close": closes / 100.0,
        "amount": amounts,
        "volume": vols,
    })
    return df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def symbol_from_path(path: Path) -> str | None:
    name = path.stem.lower()  # 如 sh000012
    if len(name) < 8:
        return None
    mkt, code = name[:2], name[2:]
    if mkt == "sh":
        return f"{code}.SH"
    if mkt == "sz":
        return f"{code}.SZ"
    return None


def main() -> None:
    vipdoc = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    cons = pd.read_csv(Path(__file__).parent / "cons_current.csv", dtype={"code": str})
    wanted = set(cons["symbol"].str.replace(".", "", regex=False).str.lower())  # sh000012
    extras = {"sh000852", "sh000300", "sh512100", "sh510300", "sh511010", "sh518880"}
    files = sorted((vipdoc / "sh" / "lday").glob("*.day")) + sorted((vipdoc / "sz" / "lday").glob("*.day"))
    n_ok = 0
    for f in files:
        key = f.stem.lower()
        if key not in wanted and key not in extras:
            continue
        sym = symbol_from_path(f)
        df = parse_day_file(f)
        if df.empty:
            continue
        df["symbol"] = sym
        df["adj_factor"] = np.nan  # 由 xdxr 管道另行重建
        df.to_parquet(out_dir / f"{sym}.parquet", index=False)
        n_ok += 1
        print(f"{sym}: {len(df)} 行 {df['date'].min().date()} ~ {df['date'].max().date()}")
    print(f"完成 {n_ok} 只")


if __name__ == "__main__":
    main()
