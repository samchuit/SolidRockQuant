"""AKShare 适配器列映射与单位换算测试（注入假 akshare 模块，不联网）.

假模块的返回结构以 2026-09 实测的 akshare 1.18.94 为准：
- 东财系（中文列）：stock_zh_a_hist / fund_etf_hist_em / index_zh_a_hist；
- 新浪系（英文列）：stock_zh_a_daily / fund_etf_hist_sina / stock_zh_index_daily；
- 新浪期货合约（英文列 hold/settle）与主连（中文列 持仓量/动态结算价）。
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from typing import Any

import numpy as np
import pandas as pd
import pytest

from solidrock.agent.errors import ErrorCode, SolidRockError
from solidrock.data.sources.akshare_source import AkshareSource
from tests.conftest import install_fake_module


def em_frame(code: str = "000001", *, scale: float = 1.0) -> pd.DataFrame:
    """东方财富系日线格式（中文列，volume=手，amount=元，换手率=%）。"""
    return pd.DataFrame(
        {
            "日期": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "股票代码": [code] * 3,
            "开盘": [10.0 * scale, 10.5 * scale, 10.2 * scale],
            "收盘": [10.4 * scale, 10.8 * scale, 10.1 * scale],
            "最高": [10.6 * scale, 11.0 * scale, 10.4 * scale],
            "最低": [9.9 * scale, 10.3 * scale, 10.0 * scale],
            "成交量": [100000.0, 120000.0, 90000.0],
            "成交额": [104000000.0 * scale, 129600000.0 * scale, 91800000.0 * scale],
            "振幅": [7.1, 6.7, 3.9],
            "涨跌幅": [1.2, 3.85, -6.48],
            "涨跌额": [0.12 * scale, 0.4 * scale, -0.7 * scale],
            "换手率": [0.65, 0.78, 0.58],
        }
    )


def sina_stock_frame(*, scale: float = 1.0) -> pd.DataFrame:
    """新浪个股日线格式（英文列，volume=股，turnover=小数）。

    close 含一次除权：1/4 相对 1/3 除权，hfq = raw × factor，
    factor = [2.0, 2.0, 2.4]（1/4 因子跳升体现分红）。
    """
    return pd.DataFrame(
        {
            "date": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "open": [10.0 * scale, 10.5 * scale, 10.2 * scale],
            "high": [10.6 * scale, 11.0 * scale, 10.4 * scale],
            "low": [9.9 * scale, 10.3 * scale, 10.0 * scale],
            "close": [10.4 * scale, 10.8 * scale, 10.1 * scale],
            "volume": [100000000.0, 120000000.0, 90000000.0],  # 股
            "amount": [104000000.0 * scale, 129600000.0 * scale, 91800000.0 * scale],
            "outstanding_share": [1.9e10] * 3,
            "turnover": [0.0065, 0.0078, 0.0058],  # 小数
        }
    )


def sina_hfq_frame() -> pd.DataFrame:
    hfq = sina_stock_frame(scale=2.0)  # 1/2、1/3 因子 2.0
    hfq.loc[2, ["open", "high", "low", "close"]] *= 1.2  # 1/4 除权 → 因子 2.4
    return hfq


def sina_fut_contract_frame() -> pd.DataFrame:
    """新版 akshare：英文列 hold/settle。"""
    return pd.DataFrame(
        {
            "date": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "open": [3800.0, 3820.0, 3790.0],
            "high": [3830.0, 3845.0, 3810.0],
            "low": [3790.0, 3800.0, 3765.0],
            "close": [3820.0, 3805.0, 3788.0],
            "volume": [120000.0, 110000.0, 95000.0],
            "hold": [2000000.0, 1990000.0, 1985000.0],
            "settle": [3812.0, 3808.0, 3789.0],
        }
    )


def sina_fut_main_frame() -> pd.DataFrame:
    """主连：中文列，且无结算价列。"""
    return pd.DataFrame(
        {
            "日期": ["2024-01-02", "2024-01-03"],
            "开盘价": [4005.0, 4048.0],
            "最高价": [4058.0, 4072.0],
            "最低价": [3983.0, 4040.0],
            "收盘价": [4047.0, 4055.0],
            "成交量": [970394.0, 964192.0],
            "持仓量": [1541082.0, 1583796.0],
            "动态结算价": [4036.0, 4055.0],
        }
    )


class FakeAk:
    """模拟 akshare 模块；EM 为主通道，新浪为回退通道。"""

    def __init__(self, *, em_broken: bool = False) -> None:
        self.em_broken = em_broken

    def _filter(self, df: pd.DataFrame, start_date: str, end_date: str, col: str = "日期") -> pd.DataFrame:
        dates = pd.to_datetime(df[col])
        mask = (dates >= pd.Timestamp(start_date)) & (dates <= pd.Timestamp(end_date))
        return df[mask].reset_index(drop=True)

    def _em_or_break(self) -> None:
        if self.em_broken:
            raise ConnectionError("em blocked")

    def stock_zh_a_hist(self, *, symbol: str, period: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        self._em_or_break()
        df = em_frame(symbol, scale=1.5 if adjust == "hfq" else 1.0)
        return self._filter(df, start_date, end_date)

    def fund_etf_hist_em(
        self, *, symbol: str, period: str, start_date: str, end_date: str, adjust: str
    ) -> pd.DataFrame:
        self._em_or_break()
        df = em_frame(symbol, scale=1.5 if adjust == "hfq" else 1.0)
        return self._filter(df, start_date, end_date)

    def index_zh_a_hist(self, *, symbol: str, period: str, start_date: str, end_date: str) -> pd.DataFrame:
        self._em_or_break()
        return self._filter(em_frame(symbol), start_date, end_date)

    # --- 新浪回退通道 ---
    def stock_zh_a_daily(self, *, symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
        assert symbol == "sz000001"
        df = sina_hfq_frame() if adjust == "hfq" else sina_stock_frame()
        return self._filter(df, start_date, end_date, col="date")

    def fund_etf_hist_sina(self, *, symbol: str) -> pd.DataFrame:
        assert symbol == "sh510300"
        df = sina_stock_frame().rename(columns={"turnover": "postVol"})
        df["postAmt"] = 1.0
        return df

    def stock_zh_index_daily(self, *, symbol: str) -> pd.DataFrame:
        assert symbol == "sh000300"
        return pd.DataFrame(
            {
                "date": ["2024-01-02", "2024-01-03", "2024-01-04"],
                "open": [3440.0, 3455.0, 3450.0],
                "high": [3458.0, 3465.0, 3455.0],
                "low": [3435.0, 3448.0, 3438.0],
                "close": [3450.0, 3460.0, 3440.0],
                "volume": [1.8e10, 1.9e10, 1.7e10],
            }
        )

    # --- 期货（新浪，无回退） ---
    def futures_zh_daily_sina(self, *, symbol: str) -> pd.DataFrame:
        assert symbol == "RB2505"
        return sina_fut_contract_frame()

    def futures_main_sina(self, *, symbol: str) -> pd.DataFrame:
        assert symbol == "RB0", "主连应转换为主连代码 RB0"
        return sina_fut_main_frame()

    # --- 参考数据 ---
    def tool_trade_date_hist_sina(self) -> pd.DataFrame:
        return pd.DataFrame({"trade_date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-06"]).date})

    def stock_info_a_code_name(self) -> pd.DataFrame:
        return pd.DataFrame({"code": ["000001", "600519", "830799"], "name": ["平安银行", "贵州茅台", "测试北交所"]})

    def index_stock_cons_csindex(self, *, symbol: str) -> pd.DataFrame:
        assert symbol == "000300"
        return pd.DataFrame({"成分券代码": ["000001", "600519"], "成分券名称": ["平安银行", "贵州茅台"]})


def _install(fake: FakeAk) -> None:
    attrs = {k: getattr(fake, k) for k in dir(fake) if not k.startswith("_")}
    install_fake_module("akshare", **attrs)


@pytest.fixture
def akshare_env() -> Iterator[FakeAk]:
    fake = FakeAk()
    _install(fake)
    yield fake
    sys.modules.pop("akshare", None)


@pytest.fixture
def src() -> AkshareSource:
    return AkshareSource()


class TestStockEm:
    def test_mapping_and_factor(self, akshare_env: FakeAk, src: AkshareSource) -> None:
        df = src.fetch_bars("000001.SZ", start="2024-01-01", end="2024-12-31")
        assert len(df) == 3
        assert set(df["symbol"]) == {"000001.SZ"}
        # pre_close = close - 涨跌额（除权后口径）
        assert df["pre_close"].iloc[1] == pytest.approx(10.8 - 0.4)
        # adj_factor = hfq_close / close = 1.5
        assert df["adj_factor"].iloc[0] == pytest.approx(1.5)
        # volume 单位为手，amount 单位为元，原样保留
        assert df["volume"].iloc[0] == pytest.approx(100_000.0)
        assert df["amount"].iloc[0] == pytest.approx(104_000_000.0)

    def test_without_adj_factor(self, akshare_env: FakeAk, src: AkshareSource) -> None:
        df = src.fetch_bars("000001.SZ", with_adj_factor=False)
        assert df["adj_factor"].isna().all()


class TestStockSinaFallback:
    def test_em_failure_falls_back(self, src: AkshareSource) -> None:
        _install(FakeAk(em_broken=True))
        try:
            df = src.fetch_bars("000001.SZ", start="2024-01-02", end="2024-01-04")
            assert len(df) == 3
            # 新浪 volume 股 → 手
            assert df["volume"].iloc[0] == pytest.approx(1_000_000.0)
            # 换手率小数 → %
            assert df["turnover_rate"].iloc[0] == pytest.approx(0.65)
            # 因子由 hfq 推得：1/2 为 2.0，1/4 除权后 2.4
            assert df["adj_factor"].iloc[0] == pytest.approx(2.0)
            assert df["adj_factor"].iloc[2] == pytest.approx(2.4)
            # 除权日 pre_close = 昨收 × 因子比 = 10.8 × 2.4/2.0 = 12.96（除权后口径）
            assert df["pre_close"].iloc[2] == pytest.approx(12.96)
            # 非除权日 pre_close = 平移昨收
            assert df["pre_close"].iloc[1] == pytest.approx(10.4)
        finally:
            sys.modules.pop("akshare", None)

    def test_both_channels_fail(self) -> None:
        def broken(**kwargs: Any) -> None:
            raise ConnectionError("down")

        _install(FakeAk(em_broken=True))
        import akshare as fake_mod

        fake_mod.stock_zh_a_daily = broken  # 新浪通道也挂
        try:
            with pytest.raises(SolidRockError) as exc_info:
                AkshareSource().fetch_bars("000001.SZ")
            assert exc_info.value.code is ErrorCode.SOURCE_REQUEST_FAILED
            assert "两条通道均失败" in exc_info.value.message
        finally:
            sys.modules.pop("akshare", None)

    def test_bj_no_fallback(self) -> None:
        _install(FakeAk(em_broken=True))
        try:
            with pytest.raises(SolidRockError) as exc_info:
                AkshareSource().fetch_bars("830799.BJ")
            # 北交所无新浪数据：不得伪成功，直接暴露东财错误
            assert exc_info.value.code is ErrorCode.SOURCE_REQUEST_FAILED
        finally:
            sys.modules.pop("akshare", None)


class TestIndexAndEtf:
    def test_index_factor_is_one(self, akshare_env: FakeAk, src: AkshareSource) -> None:
        df = src.fetch_bars("000300.SH")
        assert (df["adj_factor"] == 1.0).all()

    def test_etf(self, akshare_env: FakeAk, src: AkshareSource) -> None:
        df = src.fetch_bars("510300.SH")
        assert df["adj_factor"].iloc[0] == pytest.approx(1.5)
        assert len(df) == 3

    def test_index_sina_fallback(self) -> None:
        _install(FakeAk(em_broken=True))
        try:
            df = AkshareSource().fetch_bars("000300.SH", start="2024-01-02", end="2024-01-04")
            assert (df["close"] == [3450.0, 3460.0, 3440.0]).all()
            assert (df["adj_factor"] == 1.0).all()
        finally:
            sys.modules.pop("akshare", None)

    def test_etf_sina_fallback_volume_in_lots(self) -> None:
        _install(FakeAk(em_broken=True))
        try:
            df = AkshareSource().fetch_bars("510300.SH", start="2024-01-02", end="2024-01-04")
            # 新浪 ETF volume 股 → 手
            assert df["volume"].iloc[0] == pytest.approx(1_000_000.0)
            # 新浪 ETF 无复权 → NaN
            assert df["adj_factor"].isna().all()
        finally:
            sys.modules.pop("akshare", None)


class TestFutures:
    def test_futures_contract(self, akshare_env: FakeAk, src: AkshareSource) -> None:
        df = src.fetch_bars("RB2505.SHFE")
        assert len(df) == 3
        # 新浪无昨收：shift(1)，首行为 NaN
        assert np.isnan(df["pre_close"].iloc[0])
        assert df["pre_close"].iloc[1] == pytest.approx(3820.0)
        # 新版英文列映射：settle / hold
        assert df["settle"].iloc[0] == pytest.approx(3812.0)
        assert df["open_interest"].iloc[0] == pytest.approx(2_000_000.0)
        # 期货无复权概念
        assert (df["adj_factor"] == 1.0).all()

    def test_futures_continuous(self, akshare_env: FakeAk, src: AkshareSource) -> None:
        df = src.fetch_bars("RB.SHFE")
        assert len(df) == 2
        assert set(df["symbol"]) == {"RB.SHFE"}
        # 主连中文列：持仓量 → open_interest；无结算价列 → NaN
        assert df["open_interest"].iloc[0] == pytest.approx(1_541_082.0)
        assert df["settle"].isna().all()

    def test_date_range_filter(self, akshare_env: FakeAk, src: AkshareSource) -> None:
        df = src.fetch_bars("RB2505.SHFE", start="2024-01-03", end="2024-01-03")
        assert len(df) == 1
        assert df["date"].iloc[0] == pd.Timestamp("2024-01-03")


class TestReferenceData:
    def test_calendar(self, akshare_env: FakeAk, src: AkshareSource) -> None:
        cal = src.fetch_calendar()
        assert list(cal["date"]) == list(pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-06"]))

    def test_instruments(self, akshare_env: FakeAk, src: AkshareSource) -> None:
        df = src.fetch_instruments()
        lookup = dict(zip(df["symbol"], df["name"], strict=True))
        assert lookup["000001.SZ"] == "平安银行"
        assert lookup["600519.SH"] == "贵州茅台"
        assert lookup["830799.BJ"] == "测试北交所"

    def test_constituents(self, akshare_env: FakeAk, src: AkshareSource) -> None:
        df = src.fetch_index_constituents("000300.SH")
        assert set(df["symbol"]) == {"000001.SZ", "600519.SH"}


class TestErrors:
    def test_missing_library(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
        with pytest.raises(SolidRockError) as exc_info:
            AkshareSource().fetch_bars("000001.SZ")
        assert exc_info.value.code is ErrorCode.SOURCE_UNAVAILABLE
        assert "pip install" in exc_info.value.hint

    def test_endpoint_failure_wrapped(self) -> None:
        def boom(**kwargs: Any) -> None:
            raise ConnectionError("network down")

        # 期货为新浪单通道：失败必须包装为带 hint 的 SOURCE_REQUEST_FAILED
        install_fake_module("akshare", futures_zh_daily_sina=boom)
        try:
            with pytest.raises(SolidRockError) as exc_info:
                AkshareSource().fetch_bars("RB2505.SHFE")
            assert exc_info.value.code is ErrorCode.SOURCE_REQUEST_FAILED
            assert exc_info.value.hint
        finally:
            sys.modules.pop("akshare", None)
