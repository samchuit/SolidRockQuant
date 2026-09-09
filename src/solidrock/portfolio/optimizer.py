"""组合优化层：因子分数/预期收益 → 目标权重.

三种优化方法（scipy 求解）：
- ``min_variance``：最小方差组合
- ``mean_variance``：均值方差（最大化效用 = w'μ - λ/2 × w'Σw）
- ``risk_parity``：风险平价（等风险贡献）

所有函数输入为预期收益向量和协方差矩阵，输出为目标权重 Series
（Σ|w| = 1，无杠杆；权重下限 0 = 不做空）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from scipy.optimize import minimize

if TYPE_CHECKING:
    pass


def _validate_inputs(expected_returns: pd.Series, cov: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, list[str]]:
    symbols = list(expected_returns.index)
    common = [s for s in symbols if s in cov.index]
    if not common:
        raise ValueError("预期收益与协方差矩阵的标的不匹配")
    mu = expected_returns[common].to_numpy(dtype=float)
    sigma = cov.loc[common, common].to_numpy(dtype=float)
    return mu, sigma, common


def min_variance(cov: pd.DataFrame, *, long_only: bool = True) -> pd.Series:
    """最小方差组合."""
    sigma, symbols = cov.to_numpy(dtype=float), list(cov.index)
    n = len(symbols)
    bounds = [(0, 1)] * n if long_only else [(-1, 1)] * n
    result = minimize(
        lambda w: float(w @ sigma @ w),
        np.ones(n) / n,
        method="SLSQP",
        bounds=bounds,
        constraints=[{"type": "eq", "fun": lambda w: float(w.sum() - 1)}],
        options={"maxiter": 500},
    )
    return pd.Series(result.x, index=symbols, name="min_variance")


def mean_variance(
    expected_returns: pd.Series,
    cov: pd.DataFrame,
    *,
    risk_aversion: float = 2.0,
    long_only: bool = True,
) -> pd.Series:
    """均值方差组合：最大化 μ'w - (λ/2) × w'Σw."""
    mu, sigma, symbols = _validate_inputs(expected_returns, cov)
    n = len(symbols)
    bounds = [(0, 1)] * n if long_only else [(-1, 1)] * n

    def neg_utility(w: np.ndarray) -> float:
        return -(float(mu @ w) - risk_aversion / 2.0 * float(w @ sigma @ w))

    result = minimize(
        neg_utility,
        np.ones(n) / n,
        method="SLSQP",
        bounds=bounds,
        constraints=[{"type": "eq", "fun": lambda w: float(w.sum() - 1)}],
        options={"maxiter": 500},
    )
    return pd.Series(result.x, index=symbols, name="mean_variance")


def risk_parity(cov: pd.DataFrame) -> pd.Series:
    """风险平价组合：各标的的风险贡献相等（迭代法）.

    使用 Spinu (2013) 的固定点迭代：w_i ∝ 1/(Σw)_i，归一化。
    """
    sigma = cov.to_numpy(dtype=float)
    n = sigma.shape[0]
    w = np.ones(n) / n
    for _ in range(500):
        sigma_w = sigma @ w
        new_w = 1.0 / sigma_w
        new_w /= new_w.sum()
        if np.max(np.abs(new_w - w)) < 1e-10:
            break
        w = new_w
    symbols = list(cov.index)
    return pd.Series(w, index=symbols, name="risk_parity")
