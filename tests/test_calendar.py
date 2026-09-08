"""交易日历测试."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from solidrock.agent.errors import ErrorCode, SolidRockError
from solidrock.data.calendar import TradingCalendar
from solidrock.data.store import DataStore


class FakeCalendarSource:
    """实现 fetch_calendar 的最小假数据源。"""

    name = "fake-cal"

    def __init__(self, dates: list[str]) -> None:
        self._dates = dates

    def fetch_calendar(self, start=None, end=None):  # type: ignore[no-untyped-def]
        return pd.DataFrame({"date": pd.to_datetime(self._dates)})


@pytest.fixture
def calendar_store(tmp_path: Path) -> DataStore:
    store = DataStore(tmp_path / "data")
    store.save_calendar(pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=10)}))
    return store


class TestTradingCalendar:
    def test_is_loaded_and_coverage(self, calendar_store: DataStore) -> None:
        cal = TradingCalendar(calendar_store)
        assert cal.is_loaded
        start, end = cal.coverage()  # type: ignore[misc]
        assert start == pd.Timestamp("2024-01-01")
        assert end == pd.Timestamp("2024-01-12")

    def test_days_range(self, calendar_store: DataStore) -> None:
        cal = TradingCalendar(calendar_store)
        days = cal.days("2024-01-04", "2024-01-08")
        # 1/4(四) 1/5(五) 1/8(一)（周末剔除）
        assert list(days) == list(pd.to_datetime(["2024-01-04", "2024-01-05", "2024-01-08"]))

    def test_is_trading_day(self, calendar_store: DataStore) -> None:
        cal = TradingCalendar(calendar_store)
        assert cal.is_trading_day("2024-01-05")  # 周五
        assert not cal.is_trading_day("2024-01-06")  # 周六

    def test_prev(self, calendar_store: DataStore) -> None:
        cal = TradingCalendar(calendar_store)
        assert cal.prev("2024-01-08") == pd.Timestamp("2024-01-05")  # 跳过周末
        assert cal.prev("2024-01-08", n=2) == pd.Timestamp("2024-01-04")

    def test_next(self, calendar_store: DataStore) -> None:
        cal = TradingCalendar(calendar_store)
        assert cal.next("2024-01-05") == pd.Timestamp("2024-01-08")
        assert cal.next("2024-01-04", n=2) == pd.Timestamp("2024-01-08")

    def test_out_of_coverage(self, calendar_store: DataStore) -> None:
        cal = TradingCalendar(calendar_store)
        with pytest.raises(SolidRockError) as exc_info:
            cal.prev("2024-01-01", n=5)
        assert exc_info.value.code is ErrorCode.NO_DATA

    def test_not_loaded(self, tmp_path: Path) -> None:
        cal = TradingCalendar(DataStore(tmp_path / "empty"))
        assert not cal.is_loaded
        with pytest.raises(SolidRockError) as exc_info:
            cal.days()
        assert exc_info.value.code is ErrorCode.NO_DATA
        assert exc_info.value.hint  # 提示如何拉取日历

    def test_update_from_source(self, tmp_path: Path) -> None:
        store = DataStore(tmp_path / "data2")
        cal = TradingCalendar(store)
        n = cal.update(FakeCalendarSource(["2024-01-02", "2024-01-03"]))
        assert n == 2
        assert cal.is_loaded
        # 快照包含日历
        snap = store.create_snapshot("snap-cal")
        assert TradingCalendar(snap).is_loaded
