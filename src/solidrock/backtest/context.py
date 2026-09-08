"""策略上下文：策略与引擎之间的唯一交互界面.

提供数据查询（history）、下单（order 系列）、组合查询（portfolio）与日志。
所有下单都是**意图**：按引擎执行模式在次日开盘（默认）或当日收盘撮合，
参考价用最近收盘价。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Literal

import pandas as pd

from solidrock.agent.errors import ErrorCode, err
from solidrock.backtest.matching import Order

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from solidrock.backtest.engine import EngineState
    from solidrock.backtest.portfolio import Portfolio


class Context:
    """每个回测运行一个实例，贯穿策略生命周期."""

    def __init__(self, state: EngineState) -> None:
        self._state = state
        self.universe: list[str] = []
        self.logs: list[str] = []

    # ------------------------------------------------------------------ 属性
    @property
    def now(self) -> pd.Timestamp:
        """当前 bar 的交易日（on_signal 触发日，数据已包含当日收盘）。"""
        return self._state.now

    @property
    def params(self) -> dict:
        return self._state.params

    @property
    def portfolio(self) -> Portfolio:
        return self._state.portfolio

    # ------------------------------------------------------------------ 数据
    def history(
        self,
        symbols: str | Sequence[str],
        n: int,
        fields: str = "close",
    ) -> pd.DataFrame:
        """最近 n 根 bar 的宽表（index=date, columns=symbol），**含当前 bar**.

        ``fields`` v0.1 仅支持单字段（close/open/high/low/volume/amount 等）。
        数据不足 n 根时返回实际可得行数。
        """
        if not isinstance(fields, str):
            raise err(
                ErrorCode.PARAM_INVALID,
                "v0.1 history 仅支持单字段",
                hint="fields='close'；多字段支持计划在后续版本",
            )
        if n < 1:
            raise err(ErrorCode.PARAM_INVALID, f"n 必须 >= 1，收到 {n}")
        symbol_list = [symbols] if isinstance(symbols, str) else list(symbols)
        panel = self._state.panel
        end_idx = self._state.now_idx
        start_idx = max(0, end_idx - n + 1)
        dates = self._state.dates[start_idx : end_idx + 1]
        out = pd.DataFrame(index=dates, columns=symbol_list, dtype="float64")
        for symbol in symbol_list:
            frame = panel.get(symbol)
            if frame is None:
                continue
            series = frame.get(fields)
            if series is None:
                raise err(
                    ErrorCode.PARAM_INVALID,
                    f"字段 {fields!r} 不存在",
                    hint="可用字段：open/high/low/close/volume/amount/pre_close/adj_factor",
                )
            aligned = series.reindex(dates)
            out[symbol] = aligned.to_numpy()
        return out

    # ------------------------------------------------------------------ 下单
    def order(self, symbol: str, qty: float) -> None:
        """按股数下单：qty>0 买入（自动整手），qty<0 卖出."""
        if qty == 0:
            return
        side: Literal["buy", "sell"] = "buy" if qty > 0 else "sell"
        self._state.queue_order(Order(symbol=symbol, side=side, qty=abs(qty), created_at=self.now))

    def order_value(self, symbol: str, value: float) -> None:
        """按目标金额下单：value>0 买入，value<0 卖出（参考最近收盘价折算股数）."""
        if value == 0:
            return
        ref = self._ref_price(symbol)
        qty = abs(value) / ref
        self.order(symbol, qty if value > 0 else -qty)

    def order_target_value(self, symbol: str, value: float) -> None:
        """调整持仓到目标金额（参考最近收盘价）."""
        ref = self._ref_price(symbol)
        current = self.portfolio.position(symbol).shares
        delta = value / ref - current
        self.order(symbol, delta)

    def order_target_percent(self, symbol: str, percent: float) -> None:
        """调整持仓到目标权重（占组合总值的比例，参考最近收盘价）.

        |目标变化| < 0.5% 总值时跳过，避免频繁调仓产生无谓费用。
        """
        if percent < 0 or percent > 1:
            raise err(
                ErrorCode.PARAM_INVALID,
                f"目标权重应在 [0, 1]，收到 {percent}",
            )
        ref = self._ref_price(symbol)
        if ref <= 0 or math.isnan(ref):
            return  # 无有效价格（如上市前），跳过
        total = self.portfolio.total_value(self._state.last_prices)
        current_value = self.portfolio.position(symbol).shares * ref
        delta = percent * total - current_value
        if total > 0 and abs(delta) / total < 0.005:
            return
        self.order(symbol, delta / ref)

    def cancel_all(self) -> None:
        """撤销当日已提交、尚未执行的订单."""
        self._state.pending.clear()

    def position(self, symbol: str) -> float:
        """当前持仓股数（便捷方法）。"""
        return self.portfolio.position(symbol).shares

    # ------------------------------------------------------------------ 日志
    def log(self, message: str) -> None:
        """策略日志（进入回测产物，供 Agent 与报告引用）。"""
        self.logs.append(f"{self._state.now.date()} {message}")

    # ------------------------------------------------------------------ 内部
    def _ref_price(self, symbol: str) -> float:
        """下单参考价：最近收盘价；无数据时抛错（策略不应交易无数据标的）."""
        price = self._state.last_prices.get(symbol, float("nan"))
        if math.isnan(price):
            raise err(
                ErrorCode.NO_DATA,
                f"{symbol} 没有可用的参考价格（数据缺失或不在 universe）",
                hint="检查 ctx.universe 是否包含该符号、回测区间是否覆盖",
            )
        return price

    def push_orders(self, orders: Iterable[Order]) -> None:
        for order in orders:
            self._state.queue_order(order)
