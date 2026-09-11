"""回测风控检查.

- :class:`PositionWeightCap`：单标的权重上限，买单在撮合前按前一收盘净值封顶；
- :class:`DrawdownHalt`：净值回撤熔断——回撤超过阈值后清仓并停止开新仓
  （v0.1 单向熔断，不自动恢复）。
"""

from __future__ import annotations

import math


class PositionWeightCap:
    """单标的权重上限（默认 95%，给费用留余量）."""

    def __init__(self, max_weight: float = 0.95) -> None:
        if not 0 < max_weight <= 1:
            raise ValueError(f"max_weight 应在 (0, 1]，收到 {max_weight}")
        self.max_weight = max_weight

    def cap_qty(
        self,
        symbol: str,
        qty: float,
        ref_price: float,
        total_value: float,
        current_value: float = 0.0,
    ) -> float:
        """返回封顶后的买入股数（不整手，整手由撮合层处理）.

        ``current_value``：该标的**已有持仓市值**（持股数 × 参考价）。必须传入，
        否则反复加仓时每笔都能再买满 ``max_weight``，单标的权重上限形同虚设。
        """
        if qty <= 0 or total_value <= 0 or ref_price <= 0:
            return qty
        max_value = self.max_weight * total_value
        room = max(0.0, max_value - max(current_value, 0.0))
        return min(qty, room / ref_price)

    def is_breached(self, weight: float) -> bool:
        return weight > self.max_weight + 1e-9


class DrawdownHalt:
    """净值回撤熔断器.

    净值从峰值回撤超过 ``threshold`` 时触发；触发后引擎清仓并丢弃后续策略订单。
    """

    def __init__(self, threshold: float = 0.20) -> None:
        if not 0 < threshold < 1:
            raise ValueError(f"threshold 应在 (0, 1)，收到 {threshold}")
        self.threshold = threshold
        self.peak: float = -math.inf
        self.halted: bool = False
        self.halted_at: object = None  # pd.Timestamp

    def check(self, nav: float, now: object) -> bool:
        """更新峰值并返回是否（新近）触发熔断。"""
        self.peak = max(self.peak, nav)
        if self.halted:
            return False
        if self.peak > 0 and (self.peak - nav) / self.peak > self.threshold:
            self.halted = True
            self.halted_at = now
            return True
        return False
