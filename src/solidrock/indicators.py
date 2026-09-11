"""向量化技术指标库.

所有函数同时接受 ``pd.Series``（单标的）与 ``pd.DataFrame``（多标的宽表，
index=date, columns=symbol），纯 pandas/numpy 向量化实现，只使用当前与
历史数据（无前视）。平滑口径与通达信/MyTT 约定一致：

- ``EMA(X, N)``        = ``ewm(span=N, adjust=False)``
- ``SMA(X, N, M)``     = ``ewm(alpha=M/N, adjust=False)``（中国式移动平均）
- ``MACD`` 柱          = ``(DIF - DEA) * 2``
- ``RSI``              = 中国式 SMA 平滑（等价 Wilder）
"""

from __future__ import annotations

from typing import TypeAlias

import numpy as np
import pandas as pd

Panel: TypeAlias = "pd.Series | pd.DataFrame"

__all__ = [
    "atr",
    "bias",
    "boll",
    "cross",
    "cross_down",
    "diff",
    "ema",
    "hhv",
    "kdj",
    "llv",
    "macd",
    "ref",
    "roc",
    "rsi",
    "sma",
]


def sma(x: Panel, n: int) -> Panel:
    """简单移动平均。"""
    return x.rolling(n, min_periods=n).mean()


def ema(x: Panel, n: int) -> Panel:
    """指数移动平均（span 口径，等价通达信 EMA(X, N)）。"""
    return x.ewm(span=n, adjust=False).mean()


def ref(x: Panel, n: int = 1) -> Panel:
    """n 日前的值（前移引用）。"""
    return x.shift(n)


def diff(x: Panel, n: int = 1) -> Panel:
    """n 日差分。"""
    return x.diff(n)


def hhv(x: Panel, n: int) -> Panel:
    """n 日内最高值。"""
    return x.rolling(n, min_periods=n).max()


def llv(x: Panel, n: int) -> Panel:
    """n 日内最低值。"""
    return x.rolling(n, min_periods=n).min()


def cross(a: Panel, b: Panel) -> Panel:
    """a 上穿 b：昨日 a<=b 且今日 a>b（NaN 参与比较按 False 处理）。"""
    return (a > b) & (a.shift(1) <= b.shift(1))


def cross_down(a: Panel, b: Panel) -> Panel:
    """a 下穿 b：昨日 a>=b 且今日 a<b。"""
    return (a < b) & (a.shift(1) >= b.shift(1))


def roc(x: Panel, n: int) -> Panel:
    """n 日变动率。"""
    return x / x.shift(n) - 1.0


def bias(x: Panel, n: int) -> Panel:
    """乖离率：收盘对 n 日均线的偏离。"""
    return x / sma(x, n) - 1.0


def macd(x: Panel, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[Panel, Panel, Panel]:
    """MACD → (DIF, DEA, 柱)。柱 = (DIF - DEA) × 2（通达信口径）。"""
    dif = ema(x, fast) - ema(x, slow)
    dea = dif.ewm(span=signal, adjust=False).mean()
    return dif, dea, (dif - dea) * 2.0


def rsi(x: Panel, n: int = 14) -> Panel:
    """相对强弱指标（中国式 SMA 平滑，等价 Wilder）。

    无下跌样本（连续上涨）时取 100，无波动时取 50。
    """
    chg = x.diff()
    up = chg.clip(lower=0.0)
    dn = chg.abs()
    up_sma = up.ewm(alpha=1.0 / n, adjust=False).mean()
    dn_sma = dn.ewm(alpha=1.0 / n, adjust=False).mean()
    r = up_sma / dn_sma * 100.0
    return r.mask(dn_sma <= 1e-12, 100.0).mask((dn_sma <= 1e-12) & (up_sma <= 1e-12), 50.0)


def boll(x: Panel, n: int = 20, k: float = 2.0) -> tuple[Panel, Panel, Panel]:
    """布林带 → (中轨, 上轨, 下轨)。中轨为 n 日均线，带宽 k 倍样本标准差。"""
    mid = sma(x, n)
    std = x.rolling(n, min_periods=n).std(ddof=1)
    return mid, mid + k * std, mid - k * std


def atr(high: Panel, low: Panel, close: Panel, n: int = 14) -> Panel:
    """平均真实波幅：TR 含跳空缺口（对照昨日收盘），n 日简单平均。"""
    pre_close = close.shift(1)
    tr = (high - low).clip(lower=(high - pre_close).abs()).clip(lower=(low - pre_close).abs())
    return tr.rolling(n, min_periods=n).mean()


def kdj(high: Panel, low: Panel, close: Panel, n: int = 9, m: int = 3) -> tuple[Panel, Panel, Panel]:
    """KDJ 随机指标 → (K, D, J)。

    RSV = (C - LLV(L,N)) / (HHV(H,N) - LLV(L,N)) × 100；
    K = SMA(RSV, M, 1)，D = SMA(K, M, 1)，J = 3K - 2D。
    窗口内高低相等（一字板）时 RSV 为 NaN。
    """
    hh = hhv(high, n)
    ll = llv(low, n)
    rsv = (close - ll) / (hh - ll) * 100.0
    rsv = rsv.replace([np.inf, -np.inf], np.nan)
    k = rsv.ewm(alpha=1.0 / m, adjust=False).mean()
    d = k.ewm(alpha=1.0 / m, adjust=False).mean()
    return k, d, 3.0 * k - 2.0 * d
