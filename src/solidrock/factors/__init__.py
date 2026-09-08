"""因子研究子包."""

from solidrock.factors.analysis import FactorAnalysisResult, analyze_factor
from solidrock.factors.base import Factor, FactorData, load_factor_class
from solidrock.factors.processing import (
    neutralize,
    rank_pct,
    winsorize_mad,
    winsorize_quantile,
    zscore,
)

__all__ = [
    "Factor",
    "FactorAnalysisResult",
    "FactorData",
    "analyze_factor",
    "load_factor_class",
    "neutralize",
    "rank_pct",
    "winsorize_mad",
    "winsorize_quantile",
    "zscore",
]
