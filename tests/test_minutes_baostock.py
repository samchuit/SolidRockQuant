"""分钟线与 Baostock 适配器测试（注入假模块，不联网）."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest

from solidrock.data.sources.akshare_source import AkshareSource
from solidrock.data.sources.baostock_source import BaostockSource
from solidrock.data.store import DataStore


def em_minute_frame() -> pd.DataFrame:
    """东财分钟线（中文列，成交量=手）。"""
    return pd.DataFrame(
        {
            "时间": ["2024-01-02 09:31:00", "2024-01-02 09:32:00"],
            "开盘": [10.0, 10.1],
            "收盘": [10.1, 10.2],
            "最高": [10.2, 10.3],
            "最低": [9.9, 10.0],
            "成交量": [500.0, 600.0],
            "成交额": [50000.0, 60000.0],
        }
    )


def sina_stock_minute_frame() -> pd.DataFrame:
    """新浪个股分钟（英文列，day，成交量=股）。"""
    return pd.DataFrame(
        {
            "day": ["2024-01-02 09:31:00", "2024-01-02 09:32:00"],
            "open": [10.0, 10.1],
            "high": [10.2, 10.3],
            "low": [9.9, 10.0],
            "close": [10.1, 10.2],
            "volume": [50000.0, 60000.0],  # 股
        }
    )


def sina_fut_minute_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime": ["2024-01-02 21:01:00", "2024-01-02 21:06:00"],
            "open": [3800.0, 3801.0],
            "high": [3805.0, 3806.0],
            "low": [3795.0, 3798.0],
            "close": [3802.0, 3800.0],
            "volume": [120.0, 130.0],  # 手
            "hold": [200000.0, 200100.0],
        }
    )


class FakeMinuteAk:
    def __init__(self, *, em_broken: bool = False) -> None:
        self.em_broken = em_broken

    def stock_zh_a_hist_min_em(self, *, symbol, period, start_date, end_date, adjust):  # type: ignore[no-untyped-def]
        if self.em_broken:
            raise ConnectionError("em blocked")
        return em_minute_frame()

    def stock_zh_a_minute(self, *, symbol, period, adjust):  # type: ignore[no-untyped-def]
        assert symbol == "sz000001"
        return sina_stock_minute_frame()

    def futures_zh_minute_sina(self, *, symbol, period):  # type: ignore[no-untyped-def]
        assert symbol == "RB2505"
        assert period == "5"
        return sina_fut_minute_frame()


def _install(fake: object) -> None:
    from tests.conftest import install_fake_module

    attrs = {k: getattr(fake, k) for k in dir(fake) if not k.startswith("_")}
    install_fake_module("akshare", **attrs)


def _uninstall() -> None:
    sys.modules.pop("akshare", None)


@pytest.fixture
def minute_env() -> Iterator[None]:
    _install(FakeMinuteAk())
    yield
    _uninstall()


class TestMinutes:
    def test_stock_minute_em(self, minute_env: None) -> None:
        df = AkshareSource().fetch_bars("000001.SZ", start="2024-01-02", end="2024-01-03", freq="1m")
        assert len(df) == 2
        # 日内时间戳保留（不被归一到零点）
        assert df["date"].iloc[0] == pd.Timestamp("2024-01-02 09:31:00")
        # volume 单位为手
        assert df["volume"].iloc[0] == pytest.approx(500.0)

    def test_stock_minute_sina_fallback(self) -> None:
        _install(FakeMinuteAk(em_broken=True))
        try:
            df = AkshareSource().fetch_bars("000001.SZ", freq="5m")
            # 新浪 volume 股 → 手
            assert df["volume"].iloc[0] == pytest.approx(500.0)
            assert df["date"].iloc[0] == pd.Timestamp("2024-01-02 09:31:00")
        finally:
            _uninstall()

    def test_futures_minute(self, minute_env: None) -> None:
        df = AkshareSource().fetch_bars("RB2505.SHFE", freq="5m")
        assert df["open_interest"].iloc[0] == pytest.approx(200_000.0)
        # 夜盘时间戳保留
        assert df["date"].iloc[0] == pd.Timestamp("2024-01-02 21:01:00")

    def test_minute_store_roundtrip(self, tmp_path: Path, minute_env: None) -> None:
        store = DataStore(tmp_path / "m")
        df = AkshareSource().fetch_bars("000001.SZ", freq="1m")
        store.save_bars(df, freq="1m", source="test")
        out = store.load_bars("000001.SZ", freq="1m")
        assert out["date"].iloc[0] == pd.Timestamp("2024-01-02 09:31:00")  # 未被归一
        assert store.symbols("1m") == ["000001.SZ"]
        # 1d 与 1m 分区隔离
        assert store.symbols("1d") == []


# ------------------------------------------------------------------ Baostock
class FakeRS:
    """模拟 baostock 游标结果集。"""

    def __init__(self, rows: list[list[str]]) -> None:
        self._rows = rows
        self._i = 0
        self.error_code = "0"

    def next(self) -> bool:
        if self._i < len(self._rows):
            self._i += 1
            return True
        return False

    def get_row_data(self) -> list[str]:
        return self._rows[self._i - 1]


class FakeBs:
    def login(self):  # type: ignore[no-untyped-def]
        class R:
            error_code = "0"

        return R()

    def query_history_k_data_plus(self, *, code, fields, startdate, enddate, frequency, adjustflag):  # type: ignore[no-untyped-def]
        if frequency == "d":
            rows = [
                ["2024-01-02", "10.0", "10.6", "9.9", "10.4", "10.0", "1000000", "10400000", "0.65"],
                ["2024-01-03", "10.5", "11.0", "10.3", "10.8", "10.4", "1200000", "12960000", "0.78"],
            ]
            if adjustflag == "1":  # 后复权（仅返回 date + close×2）
                rows = [[r[0], str(float(r[4]) * 2)] for r in rows]
                return FakeRS(rows)
            return FakeRS(rows)
        # 分钟
        return FakeRS(
            [
                ["20240102093500000", "10.0", "10.2", "9.9", "10.1", "50000"],
                ["20240102094000000", "10.1", "10.3", "10.0", "10.2", "60000"],
            ]
        )

    def query_trade_dates(self, *, start_date, end_date):  # type: ignore[no-untyped-def]
        return FakeRS(
            [
                ["2024-01-01", "0"],
                ["2024-01-02", "1"],
                ["2024-01-03", "1"],
            ]
        )

    def query_hs300_stocks(self):  # type: ignore[no-untyped-def]
        return FakeRS(
            [
                ["2024-01-02", "sh.600519", "贵州茅台"],
                ["2024-01-02", "sz.000001", "平安银行"],
            ]
        )


@pytest.fixture
def baostock_env() -> Iterator[None]:
    from tests.conftest import install_fake_module

    fake = FakeBs()
    install_fake_module("baostock", **{k: getattr(fake, k) for k in dir(fake) if not k.startswith("_")})
    yield
    sys.modules.pop("baostock", None)


class TestBaostock:
    def test_daily_mapping_and_factor(self, baostock_env: None) -> None:
        df = BaostockSource().fetch_bars(["000001.SZ"], start="2024-01-01", end="2024-01-31")
        assert len(df) == 2
        # preclose 直接来自源
        assert df["pre_close"].iloc[1] == pytest.approx(10.4)
        # volume 股 → 手
        assert df["volume"].iloc[0] == pytest.approx(10_000.0)
        # 复权因子 = 后复权收盘 / 原始收盘 = 2.0
        assert df["adj_factor"].iloc[0] == pytest.approx(2.0)

    def test_calendar(self, baostock_env: None) -> None:
        cal = BaostockSource().fetch_calendar()
        assert list(cal["date"]) == [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")]

    def test_constituents(self, baostock_env: None) -> None:
        df = BaostockSource().fetch_index_constituents("000300.SH")
        assert set(df["symbol"]) == {"600519.SH", "000001.SZ"}

    def test_minute(self, baostock_env: None) -> None:
        df = BaostockSource().fetch_bars(["000001.SZ"], freq="5m")
        assert len(df) == 2
        assert df["date"].iloc[0] == pd.Timestamp("2024-01-02 09:35:00")
        assert df["volume"].iloc[0] == pytest.approx(500.0)

    def test_minute_1m_unsupported(self, baostock_env: None) -> None:
        from solidrock.agent.errors import ErrorCode, SolidRockError

        with pytest.raises(SolidRockError) as exc_info:
            BaostockSource().fetch_bars(["000001.SZ"], freq="1m")
        # 基类先做能力检查：baostock 未声明 1 分钟能力
        assert exc_info.value.code is ErrorCode.CAPABILITY_NOT_SUPPORTED

    def test_bj_unsupported(self, baostock_env: None) -> None:
        from solidrock.agent.errors import ErrorCode, SolidRockError

        with pytest.raises(SolidRockError) as exc_info:
            BaostockSource().fetch_bars(["830799.BJ"])
        assert exc_info.value.code is ErrorCode.PARAM_INVALID
