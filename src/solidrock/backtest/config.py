"""回测配置."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from solidrock.backtest.costs import CostModel
from solidrock.backtest.matching import ExecutionMode


@dataclass
class BacktestConfig:
    """回测配置."""

    start: str | pd.Timestamp
    end: str | pd.Timestamp
    benchmark: str | None = "000300.SH"  # None 关闭基准对比
    execution: ExecutionMode = "next_open"
    initial_cash: float = 1_000_000.0
    max_position_weight: float | None = 0.95  # 单标的权重上限，None 关闭
    drawdown_halt: float | None = None  # 回撤熔断阈值（如 0.2），None 关闭
    limit_ratio_overrides: dict[str, float] = field(default_factory=dict)  # 如 ST 0.05
    warmup_bars: int = 250  # start 之前预加载的 bar 数（供指标计算）
    round_lot: bool = True
    cost_model: CostModel | None = None  # None → AShareCostModel()
    name: str | None = None  # 实验名（默认策略类名）
    notes: str | None = None
    log_experiment: bool = True  # 自动写入实验追踪与产物目录

    def to_dict(self) -> dict:
        out = {
            "start": str(pd.Timestamp(self.start).date()),
            "end": str(pd.Timestamp(self.end).date()),
            "benchmark": self.benchmark,
            "execution": self.execution,
            "initial_cash": self.initial_cash,
            "max_position_weight": self.max_position_weight,
            "drawdown_halt": self.drawdown_halt,
            "warmup_bars": self.warmup_bars,
            "round_lot": self.round_lot,
            "name": self.name,
            "notes": self.notes,
            "cost_model": self.cost_model.describe() if self.cost_model is not None else None,
        }
        return out
