"""因子研究子包."""

from solidrock.factors.analysis import FactorAnalysisResult, analyze_factor
from solidrock.factors.base import (
    Factor,
    FactorData,
    create_factor,
    list_registered_factors,
    load_factor_class,
    register_factor,
)
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
    "create_factor",
    "list_registered_factors",
    "load_factor_class",
    "neutralize",
    "rank_pct",
    "register_factor",
    "winsorize_mad",
    "winsorize_quantile",
    "zscore",
]
