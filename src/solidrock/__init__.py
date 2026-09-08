"""SolidRockQuant · 磐石智擎 — Agent 原生的量化研究与回测框架.

数据层快速上手::

    from solidrock import DataStore, parse_symbol
    from solidrock.data.sources import create_source

    src = create_source("akshare")
    store = DataStore(".solidrock")
    df = src.fetch_bars(["000001.SZ", "600519.SH"], start="2024-01-01")
    store.update_bars(df, source="akshare")

回测快速上手::

    from solidrock import BacktestConfig, BacktestEngine, Strategy

    class MyStrategy(Strategy):
        ...

    result = BacktestEngine(MyStrategy, BacktestConfig(start="2024-01-01", end="2025-12-31"),
                            DataStore(".solidrock")).run()
    print(result.metrics["sharpe"])
"""

from solidrock.agent.errors import ErrorCode, SolidRockError
from solidrock.backtest import BacktestConfig, BacktestEngine, BacktestResult, Context
from solidrock.config import Settings, get_settings
from solidrock.data.store import DataStore
from solidrock.data.symbols import AssetType, Symbol, parse_symbol
from solidrock.factors import Factor, FactorData, analyze_factor
from solidrock.strategy.base import Strategy

__version__ = "0.2.0"

__all__ = [
    "AssetType",
    "BacktestConfig",
    "BacktestEngine",
    "BacktestResult",
    "Context",
    "DataStore",
    "ErrorCode",
    "Factor",
    "FactorData",
    "Settings",
    "SolidRockError",
    "Strategy",
    "Symbol",
    "__version__",
    "analyze_factor",
    "get_settings",
    "parse_symbol",
]
