"""撮合模拟：涨跌停、T+1、整手、资金约束.

执行模式（引擎配置 ``execution``）：
- ``next_open``（默认，防前视）：T 日收盘出信号 → T+1 开盘价成交；
- ``same_close``：T 日收盘出信号、当日收盘价成交（快速研究用，报告会标注）。

A股规则（每日 bar 的简化，均为业界标准做法）：
- 开盘价一字涨停（open ≥ 涨停价）→ 买单拒单；一字跌停 → 卖单拒单；
  涨停价 = pre_close × (1 + 板块比例)，四舍五入到分；板块比例：主板 10%、
  创业板(300/301)/科创板(688/689) 20%、北交所 30%，可用 ``limit_ratio_overrides`` 覆盖
  （如 ST 5%——代码段无法识别 ST，需人工指定）；
- T+1：当日买入不可卖，可卖不足直接拒单（对 Agent 更友好，不做静默部分成交）；
- 整手：买入向下取整到 100 股；卖出允许零股（清仓场景）；
- 资金不足：买单向下调整到可负担的最大整手数量，调整后为 0 则拒单；
- 停牌：当日无 bar → 订单过期（订单仅对下一根 bar 有效）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import pandas as pd

from solidrock.backtest.costs import CostModel
from solidrock.data.symbols import parse_symbol

if TYPE_CHECKING:
    from solidrock.backtest.portfolio import Portfolio

ExecutionMode = Literal["next_open", "same_close"]


@dataclass
class Order:
    """订单意图（策略在 T 日产生，T+1 开盘或当日收盘执行）."""

    symbol: str
    side: Literal["buy", "sell"]
    qty: float
    created_at: object = None  # pd.Timestamp，信号日
    source: str = "strategy"  # strategy | risk_liquidate | ...


@dataclass
class ExecutionResult:
    """撮合结果."""

    order: Order
    filled_qty: float = 0.0
    price: float = 0.0  # 含滑点成交价（raw）
    value: float = 0.0
    fees: float = 0.0
    rejected: str | None = None  # None=成交；否则为拒单码


def board_limit_ratio(symbol: str, overrides: dict[str, float] | None = None) -> float:
    """按代码段推断涨跌停比例；``overrides``（symbol → 比例）优先（如 ST 0.05）。"""
    if overrides and symbol in overrides:
        return overrides[symbol]
    sym = parse_symbol(symbol)
    if sym.exchange == "BJ":
        return 0.30
    if sym.exchange == "SH" and sym.code.startswith(("688", "689")):
        return 0.20
    if sym.exchange == "SZ" and sym.code.startswith(("300", "301")):
        return 0.20
    return 0.10


def round_half_up(value: float, digits: int = 2) -> float:
    """金融价格的四舍五入（Python 内建 round 是银行家舍入，不符合 A 股规则）。"""
    factor = 10**digits
    return math.floor(value * factor + 0.5) / factor


class ExecutionSimulator:
    """撮合器：把订单意图变成成交或拒单."""

    def __init__(
        self,
        cost_model: CostModel,
        *,
        mode: ExecutionMode = "next_open",
        limit_ratio_overrides: dict[str, float] | None = None,
        round_lot: bool = True,
    ) -> None:
        self.cost_model = cost_model
        self.mode: ExecutionMode = mode
        self.limit_ratio_overrides = limit_ratio_overrides or {}
        self.round_lot = round_lot

    # ------------------------------------------------------------------ 价格
    def limit_prices(self, symbol: str, pre_close: float) -> tuple[float, float]:
        """(涨停价, 跌停价)，四舍五入到分."""
        ratio = board_limit_ratio(symbol, self.limit_ratio_overrides)
        return (
            round_half_up(pre_close * (1 + ratio)),
            round_half_up(pre_close * (1 - ratio)),
        )

    def _ref_price(self, bar: pd.Series) -> float:
        return float(bar["open" if self.mode == "next_open" else "close"])

    # ------------------------------------------------------------------ 撮合
    def simulate_fill(self, order: Order, bar: pd.Series, portfolio: Portfolio) -> ExecutionResult:
        """对单根 bar 撮合一笔订单.

        ``bar`` 为执行日该标的的标准 bar（含 pre_close/open/close）；
        停牌（无 bar）由引擎处理，不会进入这里。
        """
        pre_close = float(bar["pre_close"])
        if pd.isna(pre_close):
            pre_close = float(bar["close"])
        limit_up, limit_down = self.limit_prices(order.symbol, pre_close)
        ref = self._ref_price(bar)
        side = order.side

        # 涨跌停检查（参考成交价已处于涨跌停位置则拒单）
        eps = 1e-6
        if side == "buy" and ref >= limit_up - eps:
            return ExecutionResult(order=order, rejected="LIMIT_UP")
        if side == "sell" and ref <= limit_down + eps:
            return ExecutionResult(order=order, rejected="LIMIT_DOWN")

        qty = order.qty
        if qty <= 0:
            return ExecutionResult(order=order, rejected="QTY_INVALID")

        if side == "sell":
            available = portfolio.position(order.symbol).available
            if qty > available + 1e-6:
                return ExecutionResult(order=order, rejected="T_PLUS_ONE", filled_qty=available)
            qty = min(qty, available)
        else:
            lot = 100.0 if self.round_lot else 1.0
            qty = math.floor(qty / lot) * lot
            if qty <= 0:
                return ExecutionResult(order=order, rejected="LOT_TOO_SMALL")
            qty = self._cap_by_cash(qty, ref, portfolio)
            if qty <= 0:
                return ExecutionResult(order=order, rejected="INSUFFICIENT_CASH")

        price = self.cost_model.slipped_price(ref, side)
        value = qty * price
        fees = self.cost_model.fees(value, side)
        return ExecutionResult(order=order, filled_qty=qty, price=price, value=value, fees=fees)

    # ------------------------------------------------------------------ 资金
    def _cap_by_cash(self, qty: float, ref_price: float, portfolio: Portfolio) -> float:
        """买单资金约束：返回可负担的最大整手数量；一手都负担不起返回 0."""
        lot = 100.0 if self.round_lot else 1.0
        cash = portfolio.cash

        def total_cost(n: float) -> float:
            value = n * self.cost_model.slipped_price(ref_price, "buy")
            return value + self.cost_model.fees(value, "buy")

        if total_cost(qty) <= cash + 1e-6:
            return qty
        # 按近似费率估算上限，再逐手回调到可负担
        approx_rate = (
            self.cost_model.commission_rate + self.cost_model.transfer_fee_rate + self.cost_model.slippage_bps / 1e4
        )
        est = math.floor(cash / (ref_price * (1 + approx_rate)) / lot) * lot
        qty = min(qty, est)
        while qty > 0 and total_cost(qty) > cash + 1e-6:
            qty -= lot
        return qty
