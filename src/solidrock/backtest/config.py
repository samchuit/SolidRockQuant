"""回测配置."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from solidrock.backtest.costs import CostModel, FuturesCostModel
from solidrock.backtest.matching import ExecutionMode


@dataclass
class BacktestConfig:
    """回测配置."""

    start: str | pd.Timestamp
    end: str | pd.Timestamp
    benchmark: str | None = "000300.SH"  # None 关闭基准对比
    execution: ExecutionMode = "next_open"
    initial_cash: float = 1_000_000.0
    max_position_weight: float | None = 0.95  # 单标的权重上限（仅股票），None 关闭
    drawdown_halt: float | None = None  # 回撤熔断阈值（如 0.2），None 关闭
    limit_ratio_overrides: dict[str, float] = field(default_factory=dict)  # 如 ST 0.05
    warmup_bars: int = 250  # start 之前预加载的 bar 数（供指标计算）
    round_lot: bool = True
    cost_model: CostModel | None = None  # None → AShareCostModel()
    futures_cost_model: FuturesCostModel | None = None  # None → 默认期货费率（universe 含期货时）
    futures_spec_overrides: dict[str, dict] = field(default_factory=dict)  # 品种/符号 → 规格覆盖
    name: str | None = None  # 实验名（默认策略类名）
    notes: str | None = None
    log_experiment: bool = True  # 自动写入实验追踪与产物目录
    carry_pending: bool = False  # 模拟盘：未执行订单携带到下一时段（回测恒为 False）

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
            "futures_cost_model": (self.futures_cost_model.describe() if self.futures_cost_model is not None else None),
            "futures_spec_overrides": self.futures_spec_overrides,
        }
        return out
