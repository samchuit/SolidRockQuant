"""Tushare 适配器列映射与单位换算测试（注入假 tushare 模块，不联网）."""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pandas as pd
import pytest

from solidrock.data.sources import tushare_source
from solidrock.data.sources.tushare_source import TushareSource


def daily_frame(descending: bool = True) -> pd.DataFrame:
    """Tushare pro.daily 格式：vol(手)、amount(千元)、trade_date 降序。

    行按日期定义（OHLC 自洽），descending=True 时模拟 Tushare 的降序返回。
    """
    rows = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ"],
            "trade_date": ["20240102", "20240103"],
            "open": [10.0, 10.5],
            "high": [10.6, 11.0],
            "low": [9.9, 10.3],
            "close": [10.4, 10.8],
            "pre_close": [10.0, 10.4],
            "change": [0.4, 0.4],
            "pct_chg": [4.0, 3.85],
            "vol": [100000.0, 120000.0],
            "amount": [104000.0, 129600.0],  # 千元
        }
    )
    return rows.iloc[::-1].reset_index(drop=True) if descending else rows


def adj_factor_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["000001.SZ"] * 2,
            "trade_date": ["20240102", "20240103"],
            "adj_factor": [1.5, 1.5],
        }
    )


def fut_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["RB2505.SHFE"] * 2,
            "trade_date": ["20240103", "20240102"],  # 降序
            "open": [3820.0, 3800.0],
            "high": [3845.0, 3830.0],
            "low": [3800.0, 3790.0],
            "close": [3805.0, 3820.0],
            "pre_settle": [3812.0, 3800.0],
            "settle": [3808.0, 3812.0],
            "vol": [110000.0, 120000.0],
            "amount": [41888.0, 45744.0],  # 万元口径
            "oi": [1_990_000.0, 2_000_000.0],
        }
    )


class FakePro:
    def daily(self, *, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return daily_frame(descending=True)

    def adj_factor(self, *, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return adj_factor_frame()

    def index_daily(self, *, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        df = daily_frame(descending=True)
        df["ts_code"] = ts_code
        return df

    def fund_daily(self, *, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return daily_frame(descending=True)

    def fund_adj(self, *, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return adj_factor_frame()

    def fut_daily(self, *, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        df = fut_frame()
        df["ts_code"] = ts_code
        return df

    def trade_cal(self, *, exchange: str, start_date: str, end_date: str, is_open: str) -> pd.DataFrame:
        assert exchange == "SSE" and is_open == "1"
        return pd.DataFrame(
            {
                "exchange": ["SSE"] * 3,
                "cal_date": ["20240106", "20240103", "20240102"],
                "is_open": ["1", "1", "1"],
            }
        )


class FailingPro(FakePro):
    def daily(self, *, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        raise RuntimeError("抱歉，您每天最多访问该接口1次（积分不足）")


@pytest.fixture
def fake_ts(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakePro]:
    fake_pro = FakePro()

    class FakeTsModule:
        def set_token(self, token: str) -> None:
            assert token == "test-token"

        def pro_api(self) -> FakePro:
            return fake_pro

    from tests.conftest import install_fake_module

    install_fake_module("tushare", set_token=FakeTsModule().set_token, pro_api=FakeTsModule().pro_api)
    yield fake_pro
    import sys

    sys.modules.pop("tushare", None)


def make_source() -> TushareSource:
    return TushareSource(token="test-token", request_interval=0)


class TestStock:
    def test_units_and_factor(self, fake_ts: FakePro) -> None:
        df = make_source().fetch_bars("000001.SZ")
        assert len(df) == 2
        # 降序输入 → 升序输出
        assert df["date"].tolist() == [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")]
        # amount 千元 → 元
        assert df["amount"].iloc[0] == pytest.approx(104_000.0 * 1000)
        # vol(手) 原样
        assert df["volume"].iloc[0] == pytest.approx(100_000.0)
        # pre_close 来自源（除权后口径）
        assert df["pre_close"].iloc[0] == pytest.approx(10.0)
        # 复权因子合并
        assert (df["adj_factor"] == 1.5).all()

    def test_without_adj_factor(self, fake_ts: FakePro) -> None:
        df = make_source().fetch_bars("000001.SZ", with_adj_factor=False)
        assert df["adj_factor"].isna().all()

    def test_index(self, fake_ts: FakePro) -> None:
        df = make_source().fetch_bars("000300.SH")
        assert (df["adj_factor"] == 1.0).all()
        assert df["amount"].iloc[0] == pytest.approx(104_000.0 * 1000)


class TestEtfAndFutures:
    def test_etf(self, fake_ts: FakePro) -> None:
        df = make_source().fetch_bars("510300.SH")
        assert (df["adj_factor"] == 1.5).all()

    def test_futures(self, fake_ts: FakePro) -> None:
        df = make_source().fetch_bars("RB2505.SHFE")
        assert df["date"].is_monotonic_increasing
        # fut_daily 无 pre_close → shift(1)，首行 NaN
        assert np.isnan(df["pre_close"].iloc[0])
        assert df["pre_close"].iloc[1] == pytest.approx(3820.0)
        # 降序输入 → 升序输出后：01-02 的 settle=3812 / oi=2_000_000
        assert df["settle"].iloc[0] == pytest.approx(3812.0)
        assert df["open_interest"].iloc[0] == pytest.approx(2_000_000.0)
        # amount 万元 → 元（TODO(unit) 已在代码中标注待核验）
        assert df["amount"].iloc[0] == pytest.approx(45744.0 * 10_000)

    def test_continuous_not_supported(self, fake_ts: FakePro) -> None:
        from solidrock.agent.errors import ErrorCode, SolidRockError

        with pytest.raises(SolidRockError) as exc_info:
            make_source().fetch_bars("RB.SHFE")
        assert exc_info.value.code is ErrorCode.PARAM_INVALID


class TestCalendar:
    def test_calendar(self, fake_ts: FakePro) -> None:
        cal = make_source().fetch_calendar()
        assert list(cal["date"]) == [
            pd.Timestamp("2024-01-02"),
            pd.Timestamp("2024-01-03"),
            pd.Timestamp("2024-01-06"),
        ]


class TestAuth:
    def test_missing_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from solidrock.agent.errors import ErrorCode, SolidRockError

        class NoTokenSettings:
            tushare_token = None

        monkeypatch.setattr(tushare_source, "get_settings", lambda: NoTokenSettings())
        with pytest.raises(SolidRockError) as exc_info:
            TushareSource()._api()
        assert exc_info.value.code is ErrorCode.SOURCE_AUTH_FAILED
        assert "SOLIDROCK_TUSHARE_TOKEN" in exc_info.value.hint

    def test_permission_error_translated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from solidrock.agent.errors import ErrorCode, SolidRockError
        from tests.conftest import install_fake_module

        class FakeTsModule:
            def set_token(self, token: str) -> None:
                pass

            def pro_api(self) -> FailingPro:
                return FailingPro()

        install_fake_module("tushare", set_token=FakeTsModule().set_token, pro_api=FakeTsModule().pro_api)
        try:
            with pytest.raises(SolidRockError) as exc_info:
                make_source().fetch_bars("000001.SZ")
            assert exc_info.value.code is ErrorCode.SOURCE_AUTH_FAILED
        finally:
            import sys

            sys.modules.pop("tushare", None)
