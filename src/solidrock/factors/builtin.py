"""内置因子库：导入即注册到因子注册表（``solidrock.factors`` 包导入时自动加载）.

命名保持简短通用（Mom/Volatility…），与 examples/ 的文件式因子（如
Momentum20）互不冲突——文件式因子按路径加载，不进注册表。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from solidrock import indicators as ta
from solidrock.factors.base import Factor, FactorData, register_factor


@register_factor
class Mom(Factor):
    """动量：过去 n 日后复权收益率（正值为动量方向）。"""

    params: dict[str, Any] = {"n": 20}
    lookback = 20

    def compute(self, data: FactorData) -> pd.DataFrame:
        n = int(self.params["n"])
        c = data.hfq_close()
        return c / c.shift(n) - 1.0


@register_factor
class Reversal(Factor):
    """短期反转：n 日动量取负（A 股短周期反转效应方向）。"""

    params: dict[str, Any] = {"n": 5}
    lookback = 5

    def compute(self, data: FactorData) -> pd.DataFrame:
        n = int(self.params["n"])
        c = data.hfq_close()
        return -(c / c.shift(n) - 1.0)


@register_factor
class Volatility(Factor):
    """波动率：n 日后复权日收益的样本标准差（低波动异象取低分位）。"""

    params: dict[str, Any] = {"n": 20}
    lookback = 21

    def compute(self, data: FactorData) -> pd.DataFrame:
        n = int(self.params["n"])
        ret = data.hfq_close().pct_change()
        return ret.rolling(n, min_periods=n).std(ddof=1)


@register_factor
class Illiq(Factor):
    """Amihud 非流动性：n 日平均 |日收益| / 成交额 × 1e9（值越大越不流动）。"""

    params: dict[str, Any] = {"n": 20}
    lookback = 21

    def compute(self, data: FactorData) -> pd.DataFrame:
        n = int(self.params["n"])
        ret = data.hfq_close().pct_change().abs()
        illiq = ret / data.amount.replace(0.0, float("nan")) * 1e9
        return illiq.rolling(n, min_periods=n).mean()


@register_factor
class VWAPDev(Factor):
    """盘中量价偏离：收盘价相对当日成交均价（amount / volume 手×100）的偏离。"""

    params: dict[str, Any] = {}
    lookback = 1

    def compute(self, data: FactorData) -> pd.DataFrame:
        vwap = (data.amount / (data.volume * 100.0)).where(data.volume > 0)
        return data.close / vwap - 1.0


@register_factor
class AtrRatio(Factor):
    """归一化波动：ATR(n) / 后复权收盘（用复权价保证除权日连续）。"""

    params: dict[str, Any] = {"n": 14}
    lookback = 15

    def compute(self, data: FactorData) -> pd.DataFrame:
        n = int(self.params["n"])
        adj = data.adj_factor.fillna(1.0)
        c = data.hfq_close()
        return ta.atr(data.high * adj, data.low * adj, c, n=n) / c
