"""共享 fixture 与测试数据构造工具."""

from __future__ import annotations

import importlib.machinery
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import numpy as np
import pandas as pd
import pytest

from solidrock.config import get_settings
from solidrock.data.store import DataStore


@pytest.fixture
def clean_settings() -> Iterator[None]:
    """隔离配置缓存：测试前后都清空 lru_cache。"""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def store(tmp_path: Path) -> DataStore:
    return DataStore(tmp_path / "data")


def make_bars(
    symbol: str = "000001.SZ",
    start: str = "2024-01-02",
    periods: int = 5,
    base: float = 10.0,
    adj_factor: float | None = 1.0,
) -> pd.DataFrame:
    """构造符合标准 schema 的测试行情。"""
    dates = pd.bdate_range(start, periods=periods)
    closes = base + np.arange(periods, dtype=float) * 0.1
    df = pd.DataFrame(
        {
            "symbol": symbol,
            "date": dates,
            "open": closes - 0.05,
            "high": closes + 0.1,
            "low": closes - 0.12,
            "close": closes,
            "pre_close": [np.nan, *closes[:-1]],
            "volume": 100_000.0,
            "amount": closes * 100_000.0,
            "turnover_rate": 1.0,
            "suspended": False,
        }
    )
    df["adj_factor"] = adj_factor if adj_factor is not None else np.nan
    return df


def install_fake_module(name: str, **attrs: object) -> ModuleType:
    """把带 __spec__ 的假模块放进 sys.modules（绕过 find_spec 检查）。"""
    import sys

    module = ModuleType(name)
    module.__spec__ = importlib.machinery.ModuleSpec(name, None)  # type: ignore[attr-defined]
    module.__path__ = []  # type: ignore[attr-defined]
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


# ------------------------------------------------------------------ 回测合成数据
def bars_frame(
    symbol: str,
    dates: pd.DatetimeIndex,
    closes: np.ndarray | list[float],
    *,
    opens: np.ndarray | list[float] | None = None,
    highs: np.ndarray | list[float] | None = None,
    lows: np.ndarray | list[float] | None = None,
    pre_closes: np.ndarray | list[float] | None = None,
    factors: np.ndarray | list[float] | None = None,
) -> pd.DataFrame:
    """构造标准 schema 的日线（默认 open=close×0.99、high/low 包络、pre_close=shift(close)）."""
    closes_arr = np.asarray(closes, dtype=float)
    opens_arr = closes_arr * 0.99 if opens is None else np.asarray(opens, dtype=float)
    highs_arr = np.maximum(opens_arr, closes_arr) * 1.01 if highs is None else np.asarray(highs, dtype=float)
    lows_arr = np.minimum(opens_arr, closes_arr) * 0.99 if lows is None else np.asarray(lows, dtype=float)
    if pre_closes is None:
        pre_arr = np.concatenate([[closes_arr[0]], closes_arr[:-1]])
    else:
        pre_arr = np.asarray(pre_closes, dtype=float)
    factor_arr = np.ones(len(closes_arr)) if factors is None else np.asarray(factors, dtype=float)
    return pd.DataFrame(
        {
            "symbol": symbol,
            "date": dates,
            "open": opens_arr,
            "high": highs_arr,
            "low": lows_arr,
            "close": closes_arr,
            "pre_close": pre_arr,
            "volume": 100_000.0,
            "amount": closes_arr * 100_000.0,
            "turnover_rate": 1.0,
            "adj_factor": factor_arr,
            "suspended": False,
        }
    )


def make_market_store(
    tmp_path: Path,
    *,
    symbols: tuple[str, ...] = ("510300.SH",),
    days: int = 30,
    start: str = "2024-01-02",
    base: float = 10.0,
    drift: float = 0.0,
    factors: np.ndarray | None = None,
    benchmark: str | None = None,
) -> DataStore:
    """确定性合成行情仓库（无随机数），含交易日历."""
    store = DataStore(tmp_path / "mkt")
    dates = pd.bdate_range(start, periods=days)
    store.save_calendar(pd.DataFrame({"date": dates}))
    frames = []
    for i, symbol in enumerate(symbols):
        closes = base + i + drift * np.arange(days)
        frames.append(bars_frame(symbol, dates, closes, factors=factors))
    if benchmark is not None:
        frames.append(bars_frame(benchmark, dates, base + 5 + drift * np.arange(days)))
    store.save_bars(pd.concat(frames, ignore_index=True), source="synthetic")
    return store
