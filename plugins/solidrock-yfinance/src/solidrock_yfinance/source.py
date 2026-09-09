"""yfinance 数据源：海外股票日线（NYSE/NASDAQ/AMEX/HKEX/TSE/LSE）.

作为插件机制（entry-points 组 ``solidrock.sources``）的官方参考实现：
不含框架侵入代码，导入即通过 ``@register_source`` 完成注册。

数据口径：
- 原始价 + 后复权因子两次取数推导（``adj_factor = hfq_close / close``），
  与框架"原始价 + 因子"约定一致；
- ``volume`` 单位为**股**（海外惯例，不除以 100）；
- 分钟线暂不支持（yfinance 1m 数据仅保留最近 30 天）。
"""

from __future__ import annotations

import importlib.util
from typing import Any

import numpy as np
import pandas as pd

from solidrock.agent.errors import ErrorCode, err
from solidrock.data.schema import DAILY_BAR_COLUMN_NAMES
from solidrock.data.sources.base import Capability, DataSource
from solidrock.data.sources.registry import register_source
from solidrock.data.symbols import Symbol


def _ensure_yf() -> Any:
    if importlib.util.find_spec("yfinance") is None:
        raise err(
            ErrorCode.SOURCE_UNAVAILABLE,
            "yfinance 插件的依赖库未安装",
            hint="pip install yfinance 或安装本插件包 solidrock-yfinance",
        )
    import yfinance as yf

    return yf


@register_source
class YfinanceSource(DataSource):
    name = "yfinance"
    capabilities = frozenset({Capability.BARS_DAILY_STOCK})

    @classmethod
    def is_available(cls) -> bool:
        return importlib.util.find_spec("yfinance") is not None

    def _fetch_bars_one(
        self,
        symbol: Symbol,
        start: pd.Timestamp | None,
        end: pd.Timestamp | None,
        *,
        with_adj_factor: bool,
    ) -> pd.DataFrame:
        yf = _ensure_yf()
        ticker = symbol.code  # yfinance 本地代码与统一符号 code 一致（如 AAPL / 7203）
        kwargs: dict[str, Any] = {"auto_adjust": False, "progress": False}
        if start is not None:
            kwargs["start"] = start.strftime("%Y-%m-%d")
        if end is not None:
            kwargs["end"] = (end + pd.Timedelta(days=1)).strftime("%Y-%m-%d")  # yfinance end 为开区间

        raw = self._request(f"日线 {symbol.value}", yf.Ticker(ticker).history, **kwargs)
        if raw is None or len(raw) == 0:
            return pd.DataFrame(columns=list(DAILY_BAR_COLUMN_NAMES))
        df = self._standardize(raw, symbol)

        if with_adj_factor:
            hfq = self._request(
                f"后复权日线 {symbol.value}", yf.Ticker(ticker).history, **{**kwargs, "auto_adjust": True}
            )
            if hfq is not None and len(hfq) > 0:
                base = df.set_index("date")["close"]
                hfq_close = pd.to_numeric(hfq["Close"], errors="coerce")
                hfq_close.index = pd.to_datetime(hfq_close.index).tz_localize(None) if hfq_close.index.tz is not None else pd.to_datetime(hfq_close.index)
                hfq_close = hfq_close.reindex(base.index)
                factor = (hfq_close / base).where(base > 0)
                df["adj_factor"] = factor.ffill().bfill().to_numpy()
        return self._slice(df, start, end)

    @staticmethod
    def _standardize(raw: pd.DataFrame, symbol: Symbol) -> pd.DataFrame:
        df = raw.rename(columns=str.lower)
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        idx = pd.to_datetime(df.index)
        df["date"] = idx.tz_localize(None) if getattr(idx, "tz", None) is not None else idx
        df = df.reset_index(drop=True)
        df["pre_close"] = df["close"].shift(1)
        df["adj_factor"] = np.nan  # 无 auto_adjust=True 数据时由调用方回填
        # 补齐标准 schema 的其余列（第三方插件样板：amount/turnover_rate 无则为 NaN）
        df["symbol"] = symbol.value
        df["suspended"] = False
        for col in ("amount", "turnover_rate", "settle", "open_interest"):
            if col not in df.columns:
                df[col] = np.nan
        return df

    @staticmethod
    def _slice(df: pd.DataFrame, start: pd.Timestamp | None, end: pd.Timestamp | None) -> pd.DataFrame:
        if df.empty:
            return df
        mask = pd.Series(True, index=df.index)
        if start is not None:
            mask &= df["date"] >= start
        if end is not None:
            mask &= df["date"] <= end
        return df.loc[mask].reset_index(drop=True)
