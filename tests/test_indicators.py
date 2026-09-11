"""P1.2 测试：向量化指标库 + 内置因子注册."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from solidrock import indicators as ta
from solidrock.factors import (
    FactorData,
    create_factor,
    list_registered_factors,
    resolve_factor,
)
from tests.conftest import bars_frame


def series(values: list[float], start: str = "2024-01-02") -> pd.Series:
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)), dtype="float64")


class TestIndicators:
    def test_sma_ema(self) -> None:
        x = series([1.0, 2.0, 3.0, 4.0, 5.0])
        assert list(ta.sma(x, 3)) == pytest.approx([np.nan, np.nan, 2.0, 3.0, 4.0], nan_ok=True)
        # EMA(span=3)：首个值参与起步，等价通达信 EMA 递推
        e = ta.ema(x, 3)
        assert e.iloc[-1] == pytest.approx(x.ewm(span=3, adjust=False).mean().iloc[-1])

    def test_ref_diff_hhv_llv(self) -> None:
        x = series([1.0, 2.0, 3.0, 4.0])
        assert list(ta.ref(x, 1)) == pytest.approx([np.nan, 1.0, 2.0, 3.0], nan_ok=True)
        assert list(ta.diff(x, 1)) == pytest.approx([np.nan, 1.0, 1.0, 1.0], nan_ok=True)
        assert list(ta.hhv(x, 2)) == pytest.approx([np.nan, 2.0, 3.0, 4.0], nan_ok=True)
        assert list(ta.llv(x, 2)) == pytest.approx([np.nan, 1.0, 2.0, 3.0], nan_ok=True)

    def test_cross(self) -> None:
        a = series([0.0, 1.0, 2.0, 3.0])
        b = series([1.0, 1.0, 1.0, 1.0])
        assert list(ta.cross(a, b).astype(float)) == pytest.approx([0.0, 0.0, 1.0, 0.0])
        assert list(ta.cross_down(b, a).astype(float)) == pytest.approx([0.0, 0.0, 1.0, 0.0])

    def test_macd(self) -> None:
        x = series(list(np.linspace(10.0, 20.0, 40)))
        dif, dea, hist = ta.macd(x)
        assert dif.iloc[-1] == pytest.approx(
            (x.ewm(span=12, adjust=False).mean() - x.ewm(span=26, adjust=False).mean()).iloc[-1]
        )
        assert hist.iloc[-1] == pytest.approx((dif - dea).iloc[-1] * 2)

    def test_rsi_bounds(self) -> None:
        up = series(list(np.linspace(10.0, 20.0, 30)))  # 单边上涨
        assert ta.rsi(up, 14).iloc[-1] == pytest.approx(100.0)
        flat = series([10.0] * 30)
        assert ta.rsi(flat, 14).iloc[-1] == pytest.approx(50.0)
        mixed = series([10.0, 11.0, 10.5, 11.5, 11.0, 12.0, 11.5, 12.5, 12.0, 13.0] * 3)
        r = ta.rsi(mixed, 14).dropna()
        assert ((r >= 0) & (r <= 100)).all()

    def test_boll(self) -> None:
        x = series(list(np.linspace(10.0, 15.0, 30)))
        mid, upper, lower = ta.boll(x, 20, 2.0)
        valid = mid.notna()
        assert (upper[valid] > mid[valid]).all()
        assert (lower[valid] < mid[valid]).all()
        np.testing.assert_allclose(mid[valid] - lower[valid], upper[valid] - mid[valid])

    def test_atr_manual(self) -> None:
        high = series([11.0, 12.0, 13.0])
        low = series([9.0, 10.0, 11.0])
        close = series([10.0, 11.0, 12.0])
        # TR = [2, 2, 2]（含对照昨日收盘的跳空口径）
        a = ta.atr(high, low, close, n=2)
        assert list(a) == pytest.approx([np.nan, 2.0, 2.0], nan_ok=True)

    def test_kdj(self) -> None:
        n = 30
        high = series(list(10.0 + np.arange(n)))
        low = series(list(9.0 + np.arange(n)))
        close = series(list(9.5 + np.arange(n)))
        k, d, j = ta.kdj(high, low, close, 9, 3)
        valid = k.notna()
        assert ((k[valid] >= 0) & (k[valid] <= 100)).all()
        assert ((d[valid] >= 0) & (d[valid] <= 100)).all()
        assert j.iloc[-1] == pytest.approx(3 * k.iloc[-1] - 2 * d.iloc[-1])

    def test_kdj_flat_is_nan(self) -> None:
        flat = series([10.0] * 15)
        k, _, _ = ta.kdj(flat, flat, flat, 9, 3)
        assert k.isna().all()

    def test_wide_dataframe(self) -> None:
        idx = pd.bdate_range("2024-01-02", periods=10)
        df = pd.DataFrame({"A": np.arange(10.0), "B": np.arange(10.0) * 2.0}, index=idx)
        out = ta.sma(df, 3)
        assert isinstance(out, pd.DataFrame)
        assert out["A"].iloc[-1] == pytest.approx(8.0)
        assert out["B"].iloc[-1] == pytest.approx(16.0)


class TestBuiltinFactors:
    def test_registered(self) -> None:
        names = set(list_registered_factors())
        assert {"Mom", "Reversal", "Volatility", "Illiq", "VWAPDev", "AtrRatio"} <= names

    def _panel(self, periods: int = 60) -> FactorData:
        dates = pd.bdate_range("2024-01-02", periods=periods)
        frames = [
            bars_frame(sym, dates, list(10.0 + 0.05 * np.arange(periods) * (i + 1)))
            for i, sym in enumerate(("000001.SZ", "600000.SH"))
        ]
        bars = pd.concat(frames, ignore_index=True)
        bars["amount"] = bars["close"] * bars["volume"] * 100.0  # 让 vwap==close
        return FactorData.from_bars(bars)

    def test_mom_compute(self) -> None:
        data = self._panel()
        f = create_factor("Mom", n=20)
        out = f.compute(data)
        c = data.hfq_close()
        expected = (c / c.shift(20) - 1.0).iloc[-1, 0]
        assert out.iloc[-1, 0] == pytest.approx(expected)

    def test_reversal_is_negative_mom(self) -> None:
        data = self._panel()
        out = create_factor("Reversal", n=5).compute(data)
        c = data.hfq_close()
        assert out.iloc[-1, 0] == pytest.approx(-(c.iloc[-1, 0] / c.iloc[-6, 0] - 1.0))

    def test_volatility_illiq_positive(self) -> None:
        data = self._panel()
        vol = create_factor("Volatility").compute(data)
        illiq = create_factor("Illiq").compute(data)
        assert (vol.iloc[-1] >= 0).all()
        assert (illiq.iloc[-1] > 0).all()

    def test_vwapdev_zero_when_vwap_is_close(self) -> None:
        data = self._panel()
        out = create_factor("VWAPDev").compute(data)
        assert out.iloc[-1, 0] == pytest.approx(0.0, abs=1e-9)

    def test_atrratio(self) -> None:
        data = self._panel()
        out = create_factor("AtrRatio", n=14).compute(data)
        assert out.iloc[-1].notna().all()
        assert (out.iloc[-1] > 0).all()

    def test_unknown_param_rejected(self) -> None:
        from solidrock.agent.errors import SolidRockError

        with pytest.raises(SolidRockError):
            create_factor("Mom", wrong=1)


class TestResolveFactor:
    def test_by_name(self) -> None:
        f = resolve_factor("Mom", n=10)
        assert f.name == "Mom"
        assert f.params["n"] == 10

    def test_by_file(self, tmp_path: Path) -> None:
        path = tmp_path / "my_factor.py"
        path.write_text(
            "from solidrock.factors import Factor, FactorData\n"
            "class MyFactor(Factor):\n"
            "    params = {'n': 3}\n"
            "    def compute(self, data):\n"
            "        return data.hfq_close() / data.hfq_close().shift(int(self.params['n'])) - 1\n",
            encoding="utf-8",
        )
        f = resolve_factor(str(path), n=7)
        assert f.name == "MyFactor"
        assert f.params["n"] == 7

    def test_unknown_name(self) -> None:
        from solidrock.agent.errors import SolidRockError

        with pytest.raises(SolidRockError):
            resolve_factor("NoSuchFactor")
