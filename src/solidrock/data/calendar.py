"""交易日历：从数据源拉取，缓存于 DataStore，提供常用查询."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from solidrock.agent.errors import ErrorCode, err

if TYPE_CHECKING:
    from solidrock.data.sources.base import DataSource
    from solidrock.data.store import DataStore


class TradingCalendar:
    """交易日历查询（首次使用前需 ``update`` 拉取一次并缓存）."""

    def __init__(self, store: DataStore) -> None:
        self._store = store
        self._days: pd.DatetimeIndex | None = self._load()

    def _load(self) -> pd.DatetimeIndex | None:
        df = self._store.load_calendar()
        if df is None or df.empty:
            return None
        days = pd.DatetimeIndex(pd.to_datetime(df["date"])).normalize().sort_values().unique()
        return pd.DatetimeIndex(days)

    @property
    def is_loaded(self) -> bool:
        return self._days is not None

    def coverage(self) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        """日历覆盖范围 (起, 止)；未加载返回 None。"""
        if self._days is None or len(self._days) == 0:
            return None
        return self._days[0], self._days[-1]

    def update(self, source: DataSource) -> int:
        """从数据源拉取并缓存日历。返回天数。"""
        cal = source.fetch_calendar()
        if cal.empty:
            raise err(
                ErrorCode.NO_DATA,
                f"数据源 {source.name!r} 未返回交易日历",
                hint="检查网络后重试，或换用其他支持 calendar 能力的数据源",
            )
        n = self._store.save_calendar(cal)
        self._days = self._load()  # 刷新内存缓存
        return n

    def _require_loaded(self) -> pd.DatetimeIndex:
        if self._days is None or len(self._days) == 0:
            raise err(
                ErrorCode.NO_DATA,
                "本地没有交易日历",
                hint="先执行 srq data calendar --update，或 TradingCalendar(store).update(create_source('akshare'))",
            )
        return self._days

    def days(self, start: str | pd.Timestamp | None = None, end: str | pd.Timestamp | None = None) -> pd.DatetimeIndex:
        """区间内全部交易日（升序）。"""
        days = self._require_loaded()
        out = days
        if start is not None:
            out = out[out >= pd.Timestamp(start).normalize()]
        if end is not None:
            out = out[out <= pd.Timestamp(end).normalize()]
        return out

    def is_trading_day(self, day: str | pd.Timestamp) -> bool:
        ts = pd.Timestamp(day).normalize()
        days = self._require_loaded()
        idx = days.searchsorted(ts)
        return idx < len(days) and days[idx] == ts

    def prev(self, day: str | pd.Timestamp, n: int = 1) -> pd.Timestamp:
        """``day`` 之前第 n 个交易日（day 本身若是交易日也不计入）。"""
        if n < 1:
            raise err(ErrorCode.PARAM_INVALID, f"n 应 >= 1，收到 {n}")
        days = self._require_loaded()
        ts = pd.Timestamp(day).normalize()
        idx = days.searchsorted(ts, side="left") - n
        if idx < 0:
            raise err(
                ErrorCode.NO_DATA,
                f"{ts.date()} 之前没有第 {n} 个交易日（日历起点 {days[0].date()}）",
                hint="更新日历以覆盖更早的历史区间",
            )
        return days[idx]

    def next(self, day: str | pd.Timestamp, n: int = 1) -> pd.Timestamp:
        """``day`` 之后第 n 个交易日（day 本身若是交易日也不计入）。"""
        if n < 1:
            raise err(ErrorCode.PARAM_INVALID, f"n 应 >= 1，收到 {n}")
        days = self._require_loaded()
        ts = pd.Timestamp(day).normalize()
        idx = days.searchsorted(ts, side="right") + n - 1
        if idx >= len(days):
            raise err(
                ErrorCode.NO_DATA,
                f"{ts.date()} 之后没有第 {n} 个交易日（日历终点 {days[-1].date()}）",
                hint="更新日历以覆盖未来的交易日区间",
            )
        return days[idx]
