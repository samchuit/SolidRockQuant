"""数据源注册表与基类通用流程测试（不依赖第三方数据源库）."""

from __future__ import annotations

import pandas as pd
import pytest
from tests.conftest import make_bars

from solidrock.agent.errors import ErrorCode, SolidRockError
from solidrock.data.sources import create_source, list_sources
from solidrock.data.sources.base import Capability, DataSource, normalize_range
from solidrock.data.sources.registry import register_source
from solidrock.data.symbols import AssetType


class StubSource(DataSource):
    name = "stub"
    capabilities = frozenset({Capability.BARS_DAILY_STOCK})

    def _fetch_bars_one(self, symbol, start, end, *, with_adj_factor):  # type: ignore[no-untyped-def]
        df = make_bars(symbol.value)
        if start is not None:
            df = df[df["date"] >= start]
        return df


@pytest.fixture
def stub_registered():
    register_source(StubSource)
    yield
    # 注册表是模块级状态；重名注册会被拒绝，同名同类则幂等，无需清理


class TestRegistry:
    def test_create_unknown_source(self) -> None:
        with pytest.raises(SolidRockError) as exc_info:
            create_source("does-not-exist")
        assert exc_info.value.code is ErrorCode.SOURCE_NOT_REGISTERED
        assert exc_info.value.hint

    def test_create_and_fetch(self, stub_registered) -> None:
        src = create_source("stub")
        df = src.fetch_bars("000001.SZ", start="2024-01-03")
        assert set(df["symbol"]) == {"000001.SZ"}
        assert (df["date"] >= pd.Timestamp("2024-01-03")).all()

    def test_duplicate_name_rejected(self, stub_registered) -> None:
        class Another(StubSource):
            pass

        with pytest.raises(ValueError, match="已被"):
            register_source(Another)

    def test_list_sources_contains_builtin(self) -> None:
        rows = {r["name"]: r for r in list_sources()}
        assert "akshare" in rows
        assert "tushare" in rows
        assert isinstance(rows["akshare"]["available"], bool)


class TestBaseFlows:
    def test_invalid_symbol_rejected(self, stub_registered) -> None:
        src = create_source("stub")
        with pytest.raises(SolidRockError) as exc_info:
            src.fetch_bars("not-a-symbol")
        assert exc_info.value.code is ErrorCode.SYMBOL_INVALID

    def test_freq_restricted(self, stub_registered) -> None:
        src = create_source("stub")
        with pytest.raises(SolidRockError) as exc_info:
            src.fetch_bars("000001.SZ", freq="2h")
        assert exc_info.value.code is ErrorCode.PARAM_INVALID

    def test_minute_capability_guard(self, stub_registered) -> None:
        src = create_source("stub")  # stub 未声明分钟能力
        with pytest.raises(SolidRockError) as exc_info:
            src.fetch_bars("000001.SZ", freq="1m")
        assert exc_info.value.code is ErrorCode.CAPABILITY_NOT_SUPPORTED

    def test_capability_guard(self, stub_registered) -> None:
        src = create_source("stub")
        with pytest.raises(SolidRockError) as exc_info:
            src.fetch_calendar()
        assert exc_info.value.code is ErrorCode.CAPABILITY_NOT_SUPPORTED
        assert "hint" in str(exc_info.value)

    def test_multiple_symbols_concat(self, stub_registered) -> None:
        src = create_source("stub")
        df = src.fetch_bars(["000001.SZ", "600519.SH"])
        assert set(df["symbol"]) == {"000001.SZ", "600519.SH"}


class TestNormalizeRange:
    def test_none_passthrough(self) -> None:
        assert normalize_range(None, None) == (None, None)

    def test_normalized_to_midnight(self) -> None:
        start, _ = normalize_range("2024-01-05 15:30", None)
        assert start == pd.Timestamp("2024-01-05")

    def test_inverted_range_rejected(self) -> None:
        with pytest.raises(SolidRockError) as exc_info:
            normalize_range("2024-02-01", "2024-01-01")
        assert exc_info.value.code is ErrorCode.PARAM_INVALID

    def test_bars_capability_mapping(self) -> None:
        assert Capability.bars_for(AssetType.STOCK) is Capability.BARS_DAILY_STOCK
        assert Capability.bars_for(AssetType.FUTURES_CONTINUOUS) is (Capability.BARS_DAILY_FUTURES_CONTINUOUS)
