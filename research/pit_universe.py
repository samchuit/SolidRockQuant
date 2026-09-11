"""PIT（point-in-time）指数成分掩码：消除幸存者偏差的共用工具.

**为什么必须有**：用"当前成分"回测历史（``cons_current.csv``）会把后来才纳入、
或从未退市的赢家提前放进宇宙，系统性高估收益；同时把已退市/已剔除的输家排除在外。
Tushare 的历史权重快照（2014-12 起 94 期）提供了逐期成分，据此构造 PIT 掩码。

口径：t 日的成分 = **trade_date ≤ t 的最近一期**快照的成分集（"最新已知成分"）。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SNAP_FILE = ROOT / "research" / "tushare" / "index_weight_000852.parquet"


def load_snapshots(path: Path | None = None) -> pd.DataFrame:
    """读取成分权重快照（columns: index_code, con_code, trade_date, weight）."""
    frame = pd.read_parquet(path or SNAP_FILE)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame.sort_values("trade_date")


def pit_membership(
    dates: pd.DatetimeIndex,
    columns: pd.Index,
    *,
    snaps: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """构造 PIT 成分布尔掩码（index=dates, columns=columns）.

    每个日期取"最近一期不晚于该日"的快照成分；快照之前无数据的日期全 False。
    返回的掩码与 ``columns`` 对齐（缺数据的成分视为当期非成分）。
    """
    snaps = load_snapshots() if snaps is None else snaps
    snap_dates = pd.DatetimeIndex(sorted(snaps["trade_date"].unique()))
    member_sets = {d: set(snaps.loc[snaps["trade_date"] == d, "con_code"]) for d in snap_dates}
    symbols = list(columns)
    rows = []
    for day in dates:
        pos = snap_dates.searchsorted(day, side="right") - 1
        members = member_sets[snap_dates[pos]] if pos >= 0 else set()
        rows.append([s in members for s in symbols])
    return pd.DataFrame(rows, index=dates, columns=columns, dtype=bool)
