"""符号解析测试."""

from __future__ import annotations

import pytest

from solidrock.agent.errors import ErrorCode, SolidRockError
from solidrock.data.symbols import (
    AssetType,
    normalize_symbol,
    parse_symbol,
    to_source_code,
)


class TestParse:
    def test_stock(self) -> None:
        s = parse_symbol("000001.SZ")
        assert s.value == "000001.SZ"
        assert s.asset_type is AssetType.STOCK

    def test_case_insensitive(self) -> None:
        assert normalize_symbol(" rb2505.shfe ") == "RB2505.SHFE"

    def test_index_sh(self) -> None:
        assert parse_symbol("000300.SH").asset_type is AssetType.INDEX

    def test_etf(self) -> None:
        assert parse_symbol("510300.SH").asset_type is AssetType.ETF
        assert parse_symbol("159915.SZ").asset_type is AssetType.ETF

    def test_futures_contract(self) -> None:
        s = parse_symbol("IF2412.CFE")
        assert s.asset_type is AssetType.FUTURES

    def test_futures_czce_three_digit(self) -> None:
        assert parse_symbol("TA505.CZCE").asset_type is AssetType.FUTURES

    def test_futures_continuous(self) -> None:
        s = parse_symbol("RB.SHFE")
        assert s.asset_type is AssetType.FUTURES_CONTINUOUS
        assert s.is_futures

    def test_bj_stock(self) -> None:
        assert parse_symbol("830799.BJ").asset_type is AssetType.STOCK


class TestInvalid:
    @pytest.mark.parametrize(
        "raw",
        ["", "000001", "000001.XX", "ABC.SZ", "RB2505.SH", "60051.SH", "60051..SH", None],
    )
    def test_invalid_raises_with_hint(self, raw: object) -> None:
        with pytest.raises(SolidRockError) as exc_info:
            parse_symbol(raw)  # type: ignore[arg-type]
        assert exc_info.value.code is ErrorCode.SYMBOL_INVALID
        assert exc_info.value.hint  # Agent 依赖 hint 自纠错


class TestSourceCode:
    def test_tushare_identity(self) -> None:
        assert to_source_code(parse_symbol("000001.SZ"), "tushare") == "000001.SZ"

    def test_akshare_em_bare_code(self) -> None:
        assert to_source_code(parse_symbol("600519.SH"), "akshare_em") == "600519"

    def test_akshare_sina_continuous_adds_zero(self) -> None:
        assert to_source_code(parse_symbol("RB.SHFE"), "akshare_sina") == "RB0"
        assert to_source_code(parse_symbol("RB2505.SHFE"), "akshare_sina") == "RB2505"

    def test_unknown_style(self) -> None:
        with pytest.raises(SolidRockError) as exc_info:
            to_source_code(parse_symbol("000001.SZ"), "wind")
        assert exc_info.value.code is ErrorCode.PARAM_INVALID
