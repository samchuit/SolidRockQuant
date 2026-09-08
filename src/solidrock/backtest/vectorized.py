"""向量化快速回测：因子值 → 权重 → 组合净值（大范围筛选用）.

与事件引擎的取舍：
- **快**：纯向量化，秒级完成数百标的×数年区间，适合因子筛选与参数扫描；
- **粗**：逐日再平衡、按收盘价成交、单一费率，不含涨跌停/T+1/保证金等约束，
  结论用于**相对比较**；精确评估请用 ``BacktestEngine``。

口径约定：
- ``weights[t]`` 表示 **t 日收盘时决定** 的目标权重，作用于 **t+1 日的收益**
  （内置 lag=1，杜绝前视）；
- 换手 = 相邻两期权重差的绝对值之和的一半（单边），成本 = 换手 × ``fee_rate``；
- 价格用**后复权收盘**（跨除权连续），等价于分红再投资。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from solidrock.agent.errors import ErrorCode, err


@dataclass
class VectorResult:
    """向量化回测结果."""

    nav: pd.Series
    port_returns: pd.Series
    turnover: pd.Series
    metrics: dict


def vectorized_backtest(
    weights: pd.DataFrame,
    close: pd.DataFrame,
    *,
    fee_rate: float = 1.5e-4,
    lag: int = 1,
) -> VectorResult:
    """按目标权重序列做向量化回测.

    ``weights``：index=date, columns=symbol 的目标权重（每日再平衡）；
    ``close``：后复权收盘宽表（与 weights 同列对齐）；
    ``fee_rate``：单边费率（佣金+冲击的合并近似）。
    """
    if lag < 1:
        raise err(ErrorCode.PARAM_INVALID, f"lag 应 >= 1（lag=1 即收盘决策次日生效），收到 {lag}")
    if fee_rate < 0:
        raise err(ErrorCode.PARAM_INVALID, f"fee_rate 应 >= 0，收到 {fee_rate}")
    common = weights.columns.intersection(close.columns)
    if common.empty:
        raise err(
            ErrorCode.PARAM_INVALID,
            "weights 与 close 没有共同标的",
            hint="两边的 columns 应为同一套统一符号",
        )
    w = weights[common].shift(lag).reindex(close.index).fillna(0.0)
    rets = close[common].pct_change()
    gross = (w * rets).sum(axis=1)
    turnover = (w - w.shift(1)).abs().sum(axis=1) / 2.0
    net = (gross - turnover * fee_rate).fillna(0.0)
    nav = (1 + net).cumprod()
    from solidrock.report.metrics import compute_metrics

    return VectorResult(nav=nav, port_returns=net, turnover=turnover, metrics=compute_metrics(nav))


def weights_from_factor(
    values: pd.DataFrame,
    *,
    top: float = 0.2,
    bottom: float = 0.2,
    gross: float = 1.0,
    min_stocks: int = 3,
) -> pd.DataFrame:
    """因子值 → 目标权重：做多因子最高 ``top`` 分位、做空最低 ``bottom`` 分位.

    等权分配后按 ``Σ|w| = gross`` 归一（默认 1.0，即名义总敞口 100%，
    多空各约 50%，市场中性）。截面有效样本 < ``min_stocks`` 的日期全 NaN。
    """
    if not 0 < top <= 1 or not 0 <= bottom <= 1:
        raise err(ErrorCode.PARAM_INVALID, f"top/bottom 应在 (0,1]，收到 top={top}, bottom={bottom}")
    if top + bottom > 1:
        raise err(ErrorCode.PARAM_INVALID, f"top + bottom 应 <= 1，收到 {top}+{bottom}")

    out = pd.DataFrame(np.nan, index=values.index, columns=values.columns, dtype="float64")
    for date, row in values.iterrows():
        valid = row.dropna()
        if len(valid) < min_stocks:
            continue
        pct = valid.rank(pct=True)
        longs = pct[pct >= 1 - top].index
        shorts = pct[pct <= bottom].index
        if len(longs) == 0 and len(shorts) == 0:
            continue
        total = 0.0
        if len(longs):
            w = 1.0 / len(longs)
            out.loc[date, longs] = w
            total += w * len(longs)
        if len(shorts):
            w = 1.0 / len(shorts)
            out.loc[date, shorts] = -w
            total += w * len(shorts)
        if gross > 0 and total > 0:
            out.loc[date] = out.loc[date] * (gross / total)
    return out
