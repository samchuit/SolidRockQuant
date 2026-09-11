"""组合核算：现金、持仓、T+1 可卖、公司行为调整.

v0.1 的公司行为处理（无分红送配数据时的近似，**价值守恒**）：

当持仓标的的复权因子相对上一 bar 发生变化（比例 ``r = factor_t / factor_prev``）时，
持仓调整为 ``shares × r``、每股成本调整为 ``avg_cost / r``。可以验证总价值
``shares × r × raw_t = shares × raw_{t-1}`` 保持连续：

- 送转/拆股（r < 1，如 10送10 时 r=0.5）：股数正确翻倍、价格减半——**完全正确**；
- 现金分红（r > 1）：表现为"分红按除权价自动再投资"，总价值连续、收益路径正确，
  但股数与真实世界略有出入（真实应得现金）。v0.2 引入分红送配数据后改为精确处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from solidrock.agent.errors import ErrorCode, err

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


@dataclass
class Position:
    """单个标的的持仓."""

    symbol: str
    shares: float = 0.0  # 总持仓（股）
    available: float = 0.0  # T+1 可卖部分（股）
    avg_cost: float = 0.0  # 每股成本（含买入费用摊入）
    last_price: float = 0.0  # 最近已知价格（raw）

    @property
    def market_value(self) -> float:
        return self.shares * self.last_price

    def unrealized_pnl(self) -> float:
        return (self.last_price - self.avg_cost) * self.shares


@dataclass
class TradeRecord:
    """一笔成交（供交易明细与胜率统计）."""

    date: object  # pd.Timestamp
    symbol: str
    side: str  # buy | sell
    qty: float
    price: float  # 含滑点的成交价（raw）
    value: float
    cost: float
    cash_after: float
    pnl: float | None = None  # 卖出时相对 avg_cost 的已实现盈亏
    reason: str = "FILL"


@dataclass
class Portfolio:
    """组合状态.

    持仓为**带符号数量**：正 = 多头，负 = 空头（期货）。两条记账路径：
    - ``buy``/``sell``：股票（全额资金、费用摊入成本、T+1 由 available 体现）；
    - ``apply_fill``：期货（保证金约束在外层、平仓释放盈亏、支持翻转）。
    ``multipliers`` 由引擎按合约规格注入（默认 1.0，股票不受影响）。
    """

    initial_cash: float
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    multipliers: dict[str, float] = field(default_factory=dict)

    def multiplier(self, symbol: str) -> float:
        return self.multipliers.get(symbol, 1.0)

    # ---------------------------------------------------------------- 查询
    def position(self, symbol: str) -> Position:
        return self.positions.get(symbol) or Position(symbol=symbol)

    @property
    def position_symbols(self) -> list[str]:
        return [s for s, p in self.positions.items() if abs(p.shares) > 1e-9]

    def market_value(self, prices: Mapping[str, float]) -> float:
        total = 0.0
        for symbol in self.position_symbols:
            pos = self.positions[symbol]
            price = prices.get(symbol, pos.last_price)
            total += pos.shares * price * self.multiplier(symbol)
        return total

    def total_value(self, prices: Mapping[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def weights(self, prices: Mapping[str, float]) -> dict[str, float]:
        total = self.total_value(prices)
        if total <= 0:
            return {}
        return {
            s: self.positions[s].shares * prices.get(s, self.positions[s].last_price) * self.multiplier(s) / total
            for s in self.position_symbols
        }

    # ---------------------------------------------------------------- 变动
    def buy(self, symbol: str, qty: float, price: float, cost: float, *, immediate_available: bool = False) -> None:
        """买入：扣现金、增持仓、摊入成本.

        ``immediate_available``：T+0 品种（如跨境/债券/黄金 ETF）当日买入
        当日可卖；默认 False（T+1，换日由 ``release_available`` 解锁）。
        """
        if qty <= 0:
            raise err(ErrorCode.PARAM_INVALID, f"买入数量必须为正，收到 {qty}")
        pos = self.positions.setdefault(symbol, Position(symbol=symbol))
        total_cost = qty * price + cost
        if total_cost > self.cash + 1e-6:
            raise err(
                ErrorCode.PARAM_INVALID,
                f"现金不足：需要 {total_cost:.2f}，可用 {self.cash:.2f}",
                hint="撮合层应先做资金校验，这是内部防线",
            )
        self.cash -= total_cost
        new_shares = pos.shares + qty
        pos.avg_cost = (pos.shares * pos.avg_cost + qty * price + cost) / new_shares
        pos.shares = new_shares
        if immediate_available:
            pos.available += qty
        pos.last_price = price

    def sell(self, symbol: str, qty: float, price: float, cost: float) -> TradeRecord:
        """卖出：现金入账、减持仓与可卖部分、记录已实现盈亏."""
        pos = self.positions.get(symbol)
        if pos is None or qty > pos.available + 1e-6:
            have = 0.0 if pos is None else pos.available
            raise err(
                ErrorCode.PARAM_INVALID,
                f"可卖数量不足：请求 {qty}，可卖 {have}",
                hint="撮合层应先做 T+1 校验，这是内部防线",
            )
        self.cash += qty * price - cost
        pnl = (price - pos.avg_cost) * qty - cost
        self.realized_pnl += pnl
        pos.shares -= qty
        pos.available -= qty
        pos.last_price = price
        if pos.shares <= 1e-9:
            del self.positions[symbol]
        return TradeRecord(
            date=None,
            symbol=symbol,
            side="sell",
            qty=qty,
            price=price,
            value=qty * price,
            cost=cost,
            cash_after=self.cash,
            pnl=pnl,
        )

    def apply_fill(self, symbol: str, qty_delta: float, price: float, cost: float) -> float:
        """期货成交核算（带符号持仓，支持开/平/翻转）.

        ``qty_delta``：正 = 买入手数，负 = 卖出手数（单位：手）。
        现金流为**带符号全额价值**（开多付价值、开空收价值，平仓反向），
        使 ``nav = cash + Σ signed_shares × price × multiplier`` 始终包含
        持仓成本基准；保证金只是引擎侧的约束，不影响现金流。
        翻转 = 先平旧仓再开新仓，费用由撮合层按开/平拆分算好一次性传入。

        返回已实现盈亏（含费用）。``avg_cost`` 为开仓价。
        """
        if abs(qty_delta) < 1e-12:
            return 0.0
        m = self.multiplier(symbol)
        pos = self.positions.setdefault(symbol, Position(symbol=symbol))
        cur = pos.shares
        closing = cur != 0 and (cur > 0) != (qty_delta > 0)
        closed = min(abs(qty_delta), abs(cur)) if closing else 0.0
        realized_gross = (
            ((price - pos.avg_cost) if cur > 0 else (pos.avg_cost - price)) * closed * m if closed > 0 else 0.0
        )
        pnl = realized_gross - cost
        self.cash -= qty_delta * price * m + cost

        new_shares = cur + qty_delta
        if abs(new_shares) < 1e-9:
            self.positions.pop(symbol, None)
            pos.shares, pos.available, pos.last_price = 0.0, 0.0, price
        else:
            if abs(qty_delta) - closed > 1e-9:  # 有新开部分 → 成本重置为成交价
                pos.avg_cost = price
            pos.shares = new_shares
            pos.last_price = price
        self.realized_pnl += pnl
        return pnl

    def release_available(self) -> None:
        """每日开盘前调用：昨日买入的股份今日可卖（T+1 解锁）."""
        for pos in self.positions.values():
            pos.available = pos.shares

    def apply_corporate_action(self, symbol: str, ratio: float) -> None:
        """复权因子变化时调整持仓（价值守恒近似，见模块 docstring）。"""
        if ratio <= 0 or abs(ratio - 1.0) < 1e-12:
            return
        pos = self.positions.get(symbol)
        if pos is None or pos.shares <= 0:
            return
        pos.shares *= ratio
        pos.available *= ratio
        pos.avg_cost /= ratio

    # ---------------------------------------------------------------- 迭代
    def __iter__(self) -> Iterator[Position]:
        return iter(self.positions.values())
