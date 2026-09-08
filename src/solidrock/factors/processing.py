"""因子截面处理：去极值、标准化、中性化（全部按日期逐行截面操作）.

输入输出均为宽表（index=date, columns=symbol），NaN 保持 NaN。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def winsorize_mad(df: pd.DataFrame, n_mad: float = 3.0) -> pd.DataFrame:
    """MAD 去极值：按中位数 ± n_mad × 1.4826×MAD 截断（逐截面）.

    MAD 为 0 的截面（多数值相同）不做截断——截到中位数会销毁全部信息。
    """
    median = df.median(axis=1)
    mad = (df.sub(median, axis=0)).abs().median(axis=1)
    upper = (median + n_mad * 1.4826 * mad).where(mad > 0, np.inf)
    lower = (median - n_mad * 1.4826 * mad).where(mad > 0, -np.inf)
    return df.clip(lower=lower, upper=upper, axis=0)


def winsorize_quantile(df: pd.DataFrame, lower: float = 0.01, upper: float = 0.99) -> pd.DataFrame:
    """分位数去极值：超出截面分位数的值截断到分位数边界。"""
    lo = df.quantile(lower, axis=1)
    hi = df.quantile(upper, axis=1)
    return df.clip(lower=lo, upper=hi, axis=0)


def zscore(df: pd.DataFrame) -> pd.DataFrame:
    """截面标准化（均值 0、标准差 1；标准差为 0 的截面保持 NaN）。"""
    mean = df.mean(axis=1)
    std = df.std(axis=1, ddof=0)
    out = df.sub(mean, axis=0)
    return out.div(std.where(std > 1e-12), axis=0)


def rank_pct(df: pd.DataFrame, ascending: bool = True) -> pd.DataFrame:
    """截面百分位排名（0~1，NaN 保持 NaN）。"""
    return df.rank(axis=1, pct=True, ascending=ascending)


def neutralize(df: pd.DataFrame, *exposures: pd.DataFrame) -> pd.DataFrame:
    """截面中性化：逐日对暴露变量做带常数项的 OLS，取残差.

    ``exposures`` 为一个或多个与 ``df`` 同形状的宽表（如市值、行业哑变量矩阵）。
    截面有效样本数 ≤ 暴露变量数 + 1 时该日返回 NaN。
    """
    out = df.copy() * np.nan
    for date in df.index:
        y = df.loc[date].dropna()
        if len(y) < 2:
            continue
        cols = []
        ok = True
        for exp in exposures:
            x = exp.loc[date].reindex(y.index)
            if x.isna().any():
                ok = False  # 暴露缺失的截面直接跳过（保守处理）
                break
            cols.append(x.to_numpy(dtype=float))
        if not ok:
            continue
        X = np.column_stack([np.ones(len(y)), *cols]) if cols else np.ones((len(y), 1))
        if X.shape[0] <= X.shape[1]:
            continue
        beta, *_ = np.linalg.lstsq(X, y.to_numpy(dtype=float), rcond=None)
        residual = y.to_numpy(dtype=float) - X @ beta
        out.loc[date, y.index] = residual
    return out
