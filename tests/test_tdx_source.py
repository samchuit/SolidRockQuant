"""通达信数据源测试（假 pytdx，不依赖行情服务器）.

重点覆盖**复权因子重建链路**——这是该适配器的核心价值（白盒 xdxr），
一旦算错会静默污染所有基于该源的回测。
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import pytest

from solidrock.agent.errors import ErrorCode, SolidRockError
from solidrock.data.schema import DAILY_BAR_COLUMN_NAMES
from solidrock.data.sources.tdx_source import TdxSource
from solidrock.data.symbols import parse_symbol
from tests.conftest import install_fake_module


def _bar(dt: str, close: float, *, open_=None, high=None, low=None, vol=1000.0, amount=1.0e6) -> dict:
    return {
        "datetime": dt,
        "open": close if open_ is None else open_,
        "high": close if high is None else high,
        "low": close if low is None else low,
        "close": close,
        "vol": vol,
        "amount": amount,
    }


def _xdxr_row(year, month, day, category, *, fenhong=0.0, songzhuangu=0.0, peigu=0.0, peigujia=0.0, suogu=np.nan):
    return {
        "year": year,
        "month": month,
        "day": day,
        "category": category,
        "fenhong": fenhong,
        "songzhuangu": songzhuangu,
        "peigu": peigu,
        "peigujia": peigujia,
        "suogu": suogu,
    }


class FakeTdxApi:
    """假 pytdx HQ API：按预置数据返回 K 线和 xdxr."""

    def __init__(self) -> None:
        self.bars: list[dict] = []
        self.xdxr: list[dict] = []
        self.connected = False
        self.connect_calls = 0
        self.fail_connect = False
        self.fail_calls = 0
        self.calls: list[tuple] = []

    def connect(self, host, port, time_out=6) -> bool:
        self.connect_calls += 1
        self.connected = not self.fail_connect
        return self.connected

    def disconnect(self) -> None:
        self.connected = False

    def get_security_bars(self, category, market, code, offset, count):
        self.calls.append(("get_security_bars", category, market, code, offset, count))
        if self.fail_calls > 0:
            self.fail_calls -= 1
            raise OSError("socket closed")
        # offset 语义：0 = 最近 count 根；翻页时向前偏移
        window = self.bars[max(0, len(self.bars) - offset - count) : len(self.bars) - offset]
        return list(window)

    def get_index_bars(self, category, market, code, offset, count):
        self.calls.append(("get_index_bars", category, market, code, offset, count))
        window = self.bars[max(0, len(self.bars) - offset - count) : len(self.bars) - offset]
        return list(window)

    def get_xdxr_info(self, market, code):
        self.calls.append(("get_xdxr_info", market, code))
        return list(self.xdxr)

    def get_security_list(self, market, offset):
        # 按市场返回不同代码（否则 SZ/SH 会被标错交易所）
        if offset > 0:
            return []
        if market == 0:  # 深市
            return [{"code": "000001", "name": "平安银行"}]
        return [{"code": "600519", "name": "贵州茅台"}]


@pytest.fixture
def tdx_env(monkeypatch: pytest.MonkeyPatch):
    """注入假 pytdx.hq；返回可配置的 api 实例制造器."""
    created: list[FakeTdxApi] = []

    class TdxHq_API:
        def __new__(cls):
            api = FakeTdxApi()
            created.append(api)
            return api

    install_fake_module("pytdx", __version__="0.0-fake")
    install_fake_module("pytdx.hq", TdxHq_API=TdxHq_API)
    yield created
    sys.modules.pop("pytdx", None)
    sys.modules.pop("pytdx.hq", None)


def _source(monkeypatch: pytest.MonkeyPatch, tmp_path, servers=None) -> TdxSource:
    # 避免读到项目真实 data_dir 里保存的服务器池
    monkeypatch.setenv("SOLIDROCK_DATA_DIR", str(tmp_path))
    from solidrock.config import get_settings

    get_settings.cache_clear()
    src = TdxSource(servers=servers or [("127.0.0.1", 7709)])
    get_settings.cache_clear()
    return src


class TestDailyBars:
    def test_schema_and_preclose(self, tdx_env, monkeypatch, tmp_path) -> None:
        src = _source(monkeypatch, tmp_path)
        src._api = FakeTdxApi()
        src._api.bars = [_bar("2024-01-02 15:00", 10.0), _bar("2024-01-03 15:00", 10.5)]
        src._api.xdxr = []
        out = src._fetch_bars_one(parse_symbol("000001.SZ"), None, None, with_adj_factor=True)
        assert list(out.columns) == list(DAILY_BAR_COLUMN_NAMES)
        assert out["symbol"].tolist() == ["000001.SZ", "000001.SZ"]
        assert out["close"].tolist() == [10.0, 10.5]
        # pre_close 初值 = 上一根收盘（首根为 NaN）
        assert pd.isna(out["pre_close"].iloc[0])
        assert out["pre_close"].iloc[1] == pytest.approx(10.0)

    def test_no_adj_factor_sets_one(self, tdx_env, monkeypatch, tmp_path) -> None:
        src = _source(monkeypatch, tmp_path)
        src._api = FakeTdxApi()
        src._api.bars = [_bar("2024-01-02 15:00", 10.0)]
        out = src._fetch_bars_one(parse_symbol("000001.SZ"), None, None, with_adj_factor=False)
        assert out["adj_factor"].iloc[0] == pytest.approx(1.0)

    def test_index_uses_index_bars(self, tdx_env, monkeypatch, tmp_path) -> None:
        src = _source(monkeypatch, tmp_path)
        src._api = FakeTdxApi()
        src._api.bars = [_bar("2024-01-02 15:00", 3000.0)]
        src._fetch_bars_one(parse_symbol("000300.SH"), None, None, with_adj_factor=False)
        assert any(c[0] == "get_index_bars" for c in src._api.calls)

    def test_bj_exchange_unsupported_returns_empty(self, tdx_env, monkeypatch, tmp_path) -> None:
        """北交所不在标准 hq 协议内：返回空表而非报错."""
        src = _source(monkeypatch, tmp_path)
        src._api = FakeTdxApi()
        out = src._fetch_bars_one(parse_symbol("430047.BJ"), None, None, with_adj_factor=False)
        assert out.empty

    def test_pagination_stops_at_start(self, tdx_env, monkeypatch, tmp_path) -> None:
        """翻页应在覆盖 start 后停止（避免无谓请求全历史）."""
        src = _source(monkeypatch, tmp_path)
        src._api = FakeTdxApi()
        from solidrock.data.sources.tdx_source import PAGE

        # 6 页数据，start 落在第 4 页附近 → 只需约 4 次请求即可停止
        n_pages = 6
        dates = pd.bdate_range("2020-01-02", periods=PAGE * n_pages)
        src._api.bars = [_bar(f"{d:%Y-%m-%d} 15:00", 10.0 + i * 0.01) for i, d in enumerate(dates)]
        start = dates[-PAGE * 2]  # 倒数第 2 页的起始日
        out = src._fetch_bars_one(parse_symbol("000001.SZ"), pd.Timestamp(start), None, with_adj_factor=False)
        assert not out.empty
        assert out["date"].min() >= pd.Timestamp(start)
        n_calls = sum(1 for c in src._api.calls if c[0] == "get_security_bars")
        assert n_calls < n_pages  # 命中 start 后提前收手，未拉满全历史


class TestXdxrAdjustment:
    """复权因子：白盒 xdxr 重建（核心正确性）."""

    def _run(self, monkeypatch, tmp_path, tdx_env, bars, xdxr):
        src = _source(monkeypatch, tmp_path)
        src._api = FakeTdxApi()
        src._api.bars = bars
        src._api.xdxr = xdxr
        return src._fetch_bars_one(parse_symbol("000001.SZ"), None, None, with_adj_factor=True)

    def test_cash_dividend(self, monkeypatch, tmp_path, tdx_env) -> None:
        """纯现金分红 10 派 5 元（d=0.5）：除权参考价 = prev_close - 0.5，因子 = prev/ref."""
        bars = [_bar("2024-06-18 15:00", 20.0), _bar("2024-06-19 15:00", 19.5)]
        xdxr = [_xdxr_row(2024, 6, 19, 1, fenhong=5.0)]
        out = self._run(monkeypatch, tmp_path, tdx_env, bars, xdxr)
        # ref = (20 - 0.5) / 1 = 19.5；因子 = 20/19.5
        assert out["adj_factor"].iloc[0] == pytest.approx(1.0)
        assert out["adj_factor"].iloc[1] == pytest.approx(20.0 / 19.5, rel=1e-9)
        # 除权后口径昨收被改写为参考价（与交易所基准一致）
        assert out["pre_close"].iloc[1] == pytest.approx(19.5)

    def test_share_split_no_cash(self, monkeypatch, tmp_path, tdx_env) -> None:
        """10 送 10（s=1）：除权参考价 = prev/2，因子 = 2（价格减半、股数翻倍）."""
        bars = [_bar("2024-06-18 15:00", 20.0), _bar("2024-06-19 15:00", 10.0)]
        xdxr = [_xdxr_row(2024, 6, 19, 1, songzhuangu=10.0)]
        out = self._run(monkeypatch, tmp_path, tdx_env, bars, xdxr)
        assert out["adj_factor"].iloc[1] == pytest.approx(2.0)
        assert out["pre_close"].iloc[1] == pytest.approx(10.0)

    def test_factor_is_stepwise_constant(self, monkeypatch, tmp_path, tdx_env) -> None:
        """因子是分段常数：事件日之后保持不变，事件之前为 1."""
        bars = [
            _bar("2024-06-18 15:00", 20.0),
            _bar("2024-06-19 15:00", 10.0),
            _bar("2024-06-20 15:00", 10.2),
            _bar("2024-06-21 15:00", 10.4),
        ]
        xdxr = [_xdxr_row(2024, 6, 19, 1, songzhuangu=10.0)]
        out = self._run(monkeypatch, tmp_path, tdx_env, bars, xdxr)
        assert out["adj_factor"].tolist() == pytest.approx([1.0, 2.0, 2.0, 2.0])

    def test_multiple_events_compound(self, monkeypatch, tmp_path, tdx_env) -> None:
        """多次除权事件因子连乘."""
        bars = [
            _bar("2024-06-18 15:00", 20.0),
            _bar("2024-06-19 15:00", 10.0),
            _bar("2024-06-20 15:00", 5.0),
        ]
        xdxr = [
            _xdxr_row(2024, 6, 19, 1, songzhuangu=10.0),  # ×2
            _xdxr_row(2024, 6, 20, 1, songzhuangu=10.0),  # ×2
        ]
        out = self._run(monkeypatch, tmp_path, tdx_env, bars, xdxr)
        assert out["adj_factor"].iloc[2] == pytest.approx(4.0)

    def test_suogu_category_11(self, monkeypatch, tmp_path, tdx_env) -> None:
        """category 11（扩缩股）：因子 = suogu."""
        bars = [_bar("2024-06-18 15:00", 10.0), _bar("2024-06-19 15:00", 5.0)]
        xdxr = [_xdxr_row(2024, 6, 19, 11, suogu=0.5)]
        out = self._run(monkeypatch, tmp_path, tdx_env, bars, xdxr)
        assert out["adj_factor"].iloc[1] == pytest.approx(0.5)

    def test_noop_event_same_day_close_unchanged(self, monkeypatch, tmp_path, tdx_env) -> None:
        """无实际调整的事件（分红送配全 0）应被忽略，因子保持 1."""
        bars = [_bar("2024-06-18 15:00", 10.0), _bar("2024-06-19 15:00", 10.0)]
        xdxr = [_xdxr_row(2024, 6, 19, 1)]
        out = self._run(monkeypatch, tmp_path, tdx_env, bars, xdxr)
        assert out["adj_factor"].tolist() == pytest.approx([1.0, 1.0])

    def test_event_outside_bar_range_ignored(self, monkeypatch, tmp_path, tdx_env) -> None:
        bars = [_bar("2024-06-18 15:00", 10.0), _bar("2024-06-19 15:00", 10.0)]
        xdxr = [_xdxr_row(2020, 1, 1, 1, songzhuangu=10.0)]  # 不在 K 线窗口内
        out = self._run(monkeypatch, tmp_path, tdx_env, bars, xdxr)
        assert out["adj_factor"].tolist() == pytest.approx([1.0, 1.0])

    def test_absurd_ratio_rejected(self, monkeypatch, tmp_path, tdx_env) -> None:
        """比值超出 (0.02, 25) 视为脏数据，不应用（防止污染整个因子序列）."""
        bars = [_bar("2024-06-18 15:00", 10.0), _bar("2024-06-19 15:00", 10.0)]
        xdxr = [_xdxr_row(2024, 6, 19, 11, suogu=0.001)]
        out = self._run(monkeypatch, tmp_path, tdx_env, bars, xdxr)
        assert out["adj_factor"].tolist() == pytest.approx([1.0, 1.0])

    def test_xdxr_failure_degrades_to_factor_one(self, monkeypatch, tmp_path, tdx_env) -> None:
        """xdxr 取不到时降级为因子 1（不因复权信息缺失而报错丢数据）."""
        src = _source(monkeypatch, tmp_path)
        api = FakeTdxApi()
        src._api = api
        api.bars = [_bar("2024-06-18 15:00", 10.0), _bar("2024-06-19 15:00", 10.0)]

        def boom(market, code):
            raise OSError("xdxr unavailable")

        api.get_xdxr_info = boom  # type: ignore[method-assign]
        out = src._fetch_bars_one(parse_symbol("000001.SZ"), None, None, with_adj_factor=True)
        assert out["adj_factor"].tolist() == pytest.approx([1.0, 1.0])
        assert len(out) == 2


class TestMinuteBars:
    def test_frequency_category_mapping(self, tdx_env, monkeypatch, tmp_path) -> None:
        """5m → category 0；1m → category 8."""
        src = _source(monkeypatch, tmp_path)
        src._api = FakeTdxApi()
        src._api.bars = [_bar("2024-01-02 09:35", 10.0)]
        src._fetch_minutes_one(parse_symbol("000001.SZ"), "5m", None, None)
        src._fetch_minutes_one(parse_symbol("000001.SZ"), "1m", None, None)
        cats = [c[1] for c in src._api.calls if c[0] == "get_security_bars"]
        assert 0 in cats and 8 in cats

    def test_minute_schema_and_volume_column(self, tdx_env, monkeypatch, tmp_path) -> None:
        src = _source(monkeypatch, tmp_path)
        src._api = FakeTdxApi()
        src._api.bars = [_bar("2024-01-02 09:35", 10.0, vol=1234.0)]
        out = src._fetch_minutes_one(parse_symbol("000001.SZ"), "5m", None, None)
        assert list(out.columns) == list(DAILY_BAR_COLUMN_NAMES)
        assert out["volume"].iloc[0] == pytest.approx(1234.0)
        # 分钟线无复权（与 akshare 适配器约定一致）
        assert pd.isna(out["adj_factor"].iloc[0])


class TestConnection:
    def test_failover_to_next_server(self, tdx_env, monkeypatch, tmp_path) -> None:
        """首台服务器连接失败时自动轮换到下一台."""
        src = _source(monkeypatch, tmp_path, servers=[("10.0.0.1", 7709), ("10.0.0.2", 7709)])
        # 第一次 connect 失败（假 api 的 fail_connect 由实例控制，这里用全部失败验证异常）
        api = FakeTdxApi()
        api.fail_connect = True
        monkeypatch.setattr(
            "solidrock.data.sources.tdx_source._ensure_pytdx",
            lambda: type("X", (), {"__new__": lambda cls: api}),
        )
        with pytest.raises(SolidRockError) as exc:
            src._connect()
        assert exc.value.code is ErrorCode.SOURCE_REQUEST_FAILED
        assert api.connect_calls == 2  # 两台各试一次

    def test_call_retries_after_failure(self, tdx_env, monkeypatch, tmp_path) -> None:
        """单次调用失败 → 重连后重试一次成功（重连拿到的是同一实例，数据仍在）."""
        src = _source(monkeypatch, tmp_path)
        shared = FakeTdxApi()
        shared.bars = [_bar("2024-01-02 15:00", 10.0)]
        shared.fail_calls = 1  # 第一次调用抛错
        monkeypatch.setattr(
            "solidrock.data.sources.tdx_source._ensure_pytdx",
            lambda: type("X", (), {"__new__": lambda cls: shared}),
        )
        src._api = shared
        result = src._call("get_security_bars", 9, 0, "000001", 0, 800)
        assert result  # 重试后拿到数据
        assert shared.connect_calls >= 1  # 确实走了重连


class TestInstruments:
    def test_instruments_both_markets(self, tdx_env, monkeypatch, tmp_path) -> None:
        src = _source(monkeypatch, tmp_path)
        src._api = FakeTdxApi()
        out = src._fetch_instruments()
        assert set(out["symbol"]) == {"000001.SZ", "600519.SH"}

    def test_is_available_reflects_import(self, tdx_env) -> None:
        assert TdxSource.is_available() is True


class TestUnsupportedMarket:
    def test_bj_market_returns_none(self, tdx_env, monkeypatch, tmp_path) -> None:
        src = _source(monkeypatch, tmp_path)
        assert src._market(parse_symbol("000001.SZ")) == 0
        assert src._market(parse_symbol("600519.SH")) == 1
        assert src._market(parse_symbol("430047.BJ")) is None
