"""数据体检测试."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from solidrock.agent.errors import ErrorCode, SolidRockError
from solidrock.data.quality import check_store, render_health_markdown
from tests.conftest import bars_frame, make_market_store


def _healthy_store(tmp_path: Path) -> object:
    return make_market_store(tmp_path, symbols=("000001.SZ", "600519.SH"), days=10)


class TestCheckStore:
    def test_healthy_data_passes(self, tmp_path: Path) -> None:
        store = _healthy_store(tmp_path)
        report = check_store(store)
        assert report.checked == 2
        assert report.healthy == 2
        assert report.ok

    def test_detects_missing_calendar_days(self, tmp_path: Path) -> None:
        store = make_market_store(tmp_path, symbols=("000001.SZ",), days=10)
        df = store.load_bars("000001.SZ")
        broken = df.drop(df.index[3])  # 缺一个交易日
        broken.to_parquet(store.bars_dir("1d") / "000001.SZ.parquet", index=False)
        report = check_store(store)
        h = report.issues[0]
        assert h.missing_calendar_days >= 1
        assert not h.ok

    def test_detects_big_jump_and_nan(self, tmp_path: Path) -> None:
        store = make_market_store(tmp_path, symbols=("000001.SZ",), days=10)
        dates = pd.bdate_range("2024-01-02", periods=10)
        closes = [10.0] * 10
        closes[5] = 13.0  # +30% 大幅波动
        broken = bars_frame("000001.SZ", dates, closes)
        broken.loc[broken.index[2], "close"] = np.nan  # NaN 收盘
        broken.to_parquet(store.bars_dir("1d") / "000001.SZ.parquet", index=False)
        report = check_store(store, jump_threshold=0.2)
        h = report.issues[0]
        assert h.nan_close == 1
        assert any(j["date"] for j in h.big_jumps)
        assert not report.ok

    def test_detects_factor_missing(self, tmp_path: Path) -> None:
        store = make_market_store(tmp_path, symbols=("510300.SH",), days=10)
        df = store.load_bars("510300.SH")
        df["adj_factor"] = np.nan  # 新浪 ETF 回退通道的典型情况
        df.to_parquet(store.bars_dir("1d") / "510300.SH.parquet", index=False)
        report = check_store(store)
        assert report.issues[0].factor_nan_ratio == pytest.approx(1.0)

    def test_render_markdown(self, tmp_path: Path) -> None:
        store = _healthy_store(tmp_path)
        report = check_store(store)
        text = render_health_markdown(report)
        assert "全部通过" in text

    def test_empty_store_raises(self, tmp_path: Path) -> None:
        from solidrock.data.store import DataStore

        with pytest.raises(SolidRockError) as exc_info:
            check_store(DataStore(tmp_path / "empty"))
        assert exc_info.value.code is ErrorCode.NO_DATA
