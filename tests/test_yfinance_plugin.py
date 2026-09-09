"""yfinance 官方插件测试（假 yfinance 模块，不联网）."""

from __future__ import annotations

import sys

import pandas as pd
import pytest

from solidrock.data.symbols import parse_symbol
from tests.conftest import install_fake_module

SYM = "AAPL.NASDAQ"


def fake_history(*, auto_adjust: bool) -> pd.DataFrame:
    scale = 2.0 if auto_adjust else 1.0
    idx = pd.DatetimeIndex(pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]).tz_localize("America/New_York"))
    return pd.DataFrame(
        {
            "Open": [100.0 * scale, 101.0 * scale, 102.0 * scale],
            "High": [101.0 * scale, 102.0 * scale, 103.0 * scale],
            "Low": [99.0 * scale, 100.0 * scale, 101.0 * scale],
            "Close": [100.5 * scale, 101.5 * scale, 102.5 * scale],
            "Volume": [1_000_000, 1_100_000, 900_000],
        },
        index=idx,
    )


class FakeTicker:
    def __init__(self, ticker: str) -> None:
        assert ticker == "AAPL", f"yfinance 本地代码应为 AAPL，收到 {ticker}"

    def history(self, *, auto_adjust: bool, **kwargs):  # type: ignore[no-untyped-def]
        return fake_history(auto_adjust=auto_adjust)


@pytest.fixture
def yf_env() -> None:

    install_fake_module("yfinance", Ticker=FakeTicker)
    yield
    sys.modules.pop("yfinance", None)


class TestSymbol:
    def test_overseas_parsing(self) -> None:
        s = parse_symbol("AAPL.NASDAQ")
        assert s.code == "AAPL"
        assert s.exchange == "NASDAQ"
        assert not s.is_futures
        assert parse_symbol("7203.TSE").code == "7203"

    def test_invalid_overseas_rejected(self) -> None:
        from solidrock.agent.errors import ErrorCode, SolidRockError

        with pytest.raises(SolidRockError) as exc_info:
            parse_symbol("BRK.A.NYSE")  # 二级后缀不支持
        assert exc_info.value.code is ErrorCode.SYMBOL_INVALID


class TestYfinancePlugin:
    def test_fetch_mapping(self, yf_env: None) -> None:
        from solidrock_yfinance.source import YfinanceSource

        df = YfinanceSource().fetch_bars([SYM], start="2024-01-01", end="2024-01-31")
        assert len(df) == 3
        # 时区剥离，date 保留日内零点
        assert df["date"].iloc[0] == pd.Timestamp("2024-01-02")
        # volume 为股（海外惯例，不除以 100）
        assert df["volume"].iloc[0] == pytest.approx(1_000_000.0)
        # 复权因子 = 后复权收盘 / 原始收盘 = 2.0
        assert df["adj_factor"].iloc[0] == pytest.approx(2.0)
        # pre_close 为原始口径昨收（shift(1)）
        assert df["pre_close"].iloc[1] == pytest.approx(100.5)

    def test_date_range_slice(self, yf_env: None) -> None:
        from solidrock_yfinance.source import YfinanceSource

        df = YfinanceSource().fetch_bars([SYM], start="2024-01-03", end="2024-01-03")
        assert len(df) == 1
        assert df["date"].iloc[0] == pd.Timestamp("2024-01-03")

    def test_dtype_after_tz_strip(self, yf_env: None) -> None:
        from solidrock_yfinance.source import YfinanceSource

        df = YfinanceSource().fetch_bars([SYM])
        assert str(df["date"].dtype) == "datetime64[ns]"
