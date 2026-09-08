"""交易费用与滑点模型（可插拔）.

所有金额以 **原始价（真实货币）** 口径计算；引擎运行在 raw 价格空间，
复权因子只用于公司行为的持仓调整，不影响费用。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from solidrock.config import get_settings

Side = Literal["buy", "sell"]


class CostModel(ABC):
    """费用模型基类：滑点作用于成交价，费用作用于成交金额.

    ``commission_rate``/``transfer_fee_rate`` 供撮合层的资金上限估算使用，
    现金类市场费用模型应提供这两个属性。
    """

    slippage_bps: float = 0.0
    commission_rate: float = 0.0
    transfer_fee_rate: float = 0.0

    def slipped_price(self, price: float, side: Side) -> float:
        """按方向施加滑点：买入抬高、卖出压低（对称 bps）。"""
        slip = self.slippage_bps / 1e4
        return price * (1 + slip) if side == "buy" else price * (1 - slip)

    @abstractmethod
    def fees(self, value: float, side: Side, *, closing_today_value: float = 0.0) -> float:
        """给定成交金额（已含滑点）与方向，返回总费用.

        ``closing_today_value``：平仓金额中"当日开仓"部分（期货平今差别费率
        用）；股票模型忽略。
        """

    def describe(self) -> dict[str, float]:
        """模型参数摘要（实验记录用）。"""
        return {"slippage_bps": self.slippage_bps}


class AShareCostModel(CostModel):
    """A 股费用模型（默认值取全局配置，均可覆盖）.

    - 佣金：双边，``max(value × commission_rate, commission_min)``；
    - 印花税：仅卖出，``value × stamp_duty_rate``；
    - 过户费：双边，``value × transfer_fee_rate``；
    - 滑点：bps，作用于成交价。
    """

    def __init__(
        self,
        *,
        commission_rate: float | None = None,
        commission_min: float | None = None,
        stamp_duty_rate: float | None = None,
        transfer_fee_rate: float | None = None,
        slippage_bps: float | None = None,
    ) -> None:
        s = get_settings()
        self.commission_rate = s.commission_rate if commission_rate is None else commission_rate
        self.commission_min = s.commission_min if commission_min is None else commission_min
        self.stamp_duty_rate = s.stamp_duty_rate if stamp_duty_rate is None else stamp_duty_rate
        self.transfer_fee_rate = s.transfer_fee_rate if transfer_fee_rate is None else transfer_fee_rate
        self.slippage_bps = s.slippage_bps if slippage_bps is None else slippage_bps

    def fees(self, value: float, side: Side, *, closing_today_value: float = 0.0) -> float:
        if value <= 0:
            return 0.0
        commission = max(value * self.commission_rate, self.commission_min)
        stamp = value * self.stamp_duty_rate if side == "sell" else 0.0
        transfer = value * self.transfer_fee_rate
        return commission + stamp + transfer

    def describe(self) -> dict[str, float]:
        return {
            "commission_rate": self.commission_rate,
            "commission_min": self.commission_min,
            "stamp_duty_rate": self.stamp_duty_rate,
            "transfer_fee_rate": self.transfer_fee_rate,
            "slippage_bps": self.slippage_bps,
        }


class FuturesCostModel(CostModel):
    """期货费用模型：开仓/平昨/平今三段费率（费率 × 合约价值）.

    合约价值 = 价格 × 手数 × 合约乘数（引擎算好传入 ``value``）。
    平仓时 ``closing_today_value`` 部分按平今费率、其余按平昨费率；
    开仓一律按开仓费率。各品种费率差异大，请按合约规格覆盖。
    """

    def __init__(
        self,
        *,
        open_fee_rate: float = 1e-4,
        close_fee_rate: float = 1e-4,
        close_today_fee_rate: float = 5e-4,
        slippage_bps: float = 1.0,
    ) -> None:
        self.open_fee_rate = open_fee_rate
        self.close_fee_rate = close_fee_rate
        self.close_today_fee_rate = close_today_fee_rate
        self.slippage_bps = slippage_bps

    def fees(self, value: float, side: Side, *, closing_today_value: float = 0.0) -> float:
        """单边近似计费（side=buy 视为开仓、sell 视为平仓）.

        期货的开/平由持仓方向决定而非买卖方向，混合开平请用
        :meth:`trade_fees`（撮合层期货路径使用）。
        """
        if value <= 0:
            return 0.0
        if side == "buy":
            return value * self.open_fee_rate
        today = min(max(closing_today_value, 0.0), value)
        return value * self.close_fee_rate + today * (self.close_today_fee_rate - self.close_fee_rate)

    def trade_fees(self, *, open_value: float = 0.0, close_value: float = 0.0, close_today_value: float = 0.0) -> float:
        """精确计费：按开仓/平昨/平今三段金额分别计价（撮合层期货路径调用）."""
        close_ordinary = max(close_value - max(close_today_value, 0.0), 0.0)
        today = min(max(close_today_value, 0.0), close_value)
        return (
            open_value * self.open_fee_rate + close_ordinary * self.close_fee_rate + today * self.close_today_fee_rate
        )

    def describe(self) -> dict[str, float]:
        return {
            "open_fee_rate": self.open_fee_rate,
            "close_fee_rate": self.close_fee_rate,
            "close_today_fee_rate": self.close_today_fee_rate,
            "slippage_bps": self.slippage_bps,
        }
