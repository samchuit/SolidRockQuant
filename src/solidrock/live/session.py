r"""策略驱动实盘会话.

复用策略 API（:class:`~solidrock.strategy.base.Strategy` +
:class:`~solidrock.backtest.context.Context`）与本地数据仓库，把策略的
**意图订单**翻译为 QMT 实盘委托。与回测同一套策略代码——区别只在
数据快照是最新本地日线、组合状态来自 QMT 实时查询。

设计约定：

- **数据**：本地 DataStore 日线 warmup（开会话前先 ``srq data update``），
  策略看到的 ``ctx.history`` 与回测完全同构（同一 schema、同一符号）；
- **组合快照**：每次阶段运行从 QMT 查询资金/持仓构建（字段防御式映射，
  以 QMT 返回为准）；broker 缺省时用 ``paper_cash`` 模拟组合（纯试跑）；
- **订单**：策略下单进入 pending → 会话统一翻译为实盘委托（买入整手
  向下取整；市价 LATEST_PRICE 或按 ``limit_pct`` 以最近收盘价挂限价），
  逐笔写入审计日志（``{data_dir}/live/audit.jsonl``）；只读模式下 broker
  直接拒绝下单；
- **安全**：默认 dry-run（只产生意图清单，不下单）；真实下单需 CLI
  ``--execute`` 且关闭只读；每阶段产物落盘
  ``{data_dir}/live/sessions/{date}_{phase}.json``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import pandas as pd

from solidrock.agent.errors import ErrorCode, err
from solidrock.backtest.context import Context
from solidrock.backtest.engine import EngineState
from solidrock.backtest.matching import Order
from solidrock.backtest.portfolio import Portfolio, Position
from solidrock.data.calendar import TradingCalendar

if TYPE_CHECKING:
    from solidrock.data.store import DataStore
    from solidrock.live.broker import CfquantBroker
    from solidrock.strategy.base import Strategy

_ROUND_LOT = 100


@dataclass
class SessionConfig:
    """实盘会话配置."""

    warmup_bars: int = 250  # 策略 history 所需历史 bar 数
    paper_cash: float = 1_000_000.0  # broker 缺省（dry-run 无 QMT）时的模拟资金
    limit_pct: float | None = None  # None → 市价；如 0.001 → 限价 close*(1±pct)
    name: str = "live-session"


@dataclass
class IntendedOrder:
    """策略意图订单的实盘翻译结果（dry-run 时 submitted=False）."""

    symbol: str
    side: str
    qty: int
    price: float | None
    submitted: bool = False
    order_id: Any = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "price": self.price,
            "submitted": self.submitted,
            "order_id": self.order_id,
            "note": self.note,
        }


class LiveSession:
    """单阶段实盘会话执行器（open/signal/close 三阶段，可由 CLI/调度器驱动）."""

    def __init__(
        self,
        strategy: Strategy,
        store: DataStore,
        broker: CfquantBroker | None = None,
        *,
        config: SessionConfig | None = None,
    ) -> None:
        self.strategy = strategy
        self.store = store
        self.broker = broker
        self.cfg = config or SessionConfig()

    # ------------------------------------------------------------------ 阶段
    def run_phase(self, phase: str, now: pd.Timestamp | None = None, *, execute: bool = False) -> dict[str, Any]:
        """运行一个阶段：加载快照 → 调策略钩子 → 翻译并（可选）提交订单.

        ``phase``：``open``（on_market_open）/ ``signal``（on_signal）/
        ``close``（on_market_close）。``execute=False`` 时只产出意图清单。
        """
        if phase not in ("open", "signal", "close"):
            raise err(ErrorCode.PARAM_INVALID, f"phase 应为 open/signal/close，收到 {phase!r}")
        now = (now or pd.Timestamp.now()).normalize() if phase != "signal" else (now or pd.Timestamp.now())
        state, ctx = self._build_state(now)
        # 策略看到的历史截至本地日线最新一根（盘中调用时通常为上一交易日收盘）
        hook = {
            "open": self.strategy.on_market_open,
            "signal": self.strategy.on_signal,
            "close": self.strategy.on_market_close,
        }[phase]
        hook(ctx)
        orders = [self._translate(o, state.last_prices, execute) for o in state.pending]
        submitted = [o.to_dict() for o in orders if o.submitted]
        summary = {
            "phase": phase,
            "now": str(now),
            "strategy": type(self.strategy).__name__,
            "universe": ctx.universe,
            "orders": [o.to_dict() for o in orders],
            "submitted": submitted,
            "logs": ctx.logs,
        }
        self._save_summary(summary)
        return summary

    # ------------------------------------------------------------------ 状态构建
    def _build_state(self, now: pd.Timestamp) -> tuple[EngineState, Context]:
        state = EngineState(dates=[], panel={})
        ctx = Context(state)
        self.strategy.setup(ctx)
        universe = list(ctx.universe)
        if not universe:
            raise err(
                ErrorCode.PARAM_INVALID,
                "策略未设置 ctx.universe",
                hint="在 setup() 中设置 ctx.universe = ['510300.SH', ...]",
            )
        end = now.normalize() + pd.Timedelta(days=1) if now.time() > pd.Timestamp("15:00").time() else now.normalize()
        # 全量加载本地日线（history 从末端切片取 n 根）；start 不锚定会话时间，
        # 避免本地数据早于 warmup 窗口时取不到任何 bar
        bars = self.store.load_bars(universe, end=end)
        if bars.empty:
            raise err(
                ErrorCode.NO_DATA,
                f"universe {universe[:5]} 无本地日线数据",
                hint="先执行 srq data update 更新数据再开会话",
            )
        panel: dict[str, pd.DataFrame] = {}
        for symbol, part in bars.groupby("symbol"):
            panel[str(symbol)] = part.drop(columns=["symbol"]).set_index("date").sort_index()
        dates = sorted({ts for frame in panel.values() for ts in frame.index})
        state.panel = panel
        state.dates = list(dates)
        state.now = dates[-1]
        state.now_idx = len(dates) - 1
        state.params = dict(self.strategy.params)
        last_frame_day = dates[-1]
        state.last_prices = {
            s: float(frame["close"].loc[last_frame_day])
            for s, frame in panel.items()
            if last_frame_day in frame.index and not pd.isna(frame["close"].loc[last_frame_day])
        }
        state.portfolio = self._portfolio_snapshot(state.last_prices)
        return state, ctx

    def _portfolio_snapshot(self, last_prices: dict[str, float]) -> Portfolio:
        """从 QMT 查询构建组合快照；broker 缺省时用 paper_cash 模拟."""
        if self.broker is None:
            return Portfolio(initial_cash=self.cfg.paper_cash, cash=self.cfg.paper_cash)
        asset = self.broker.query_asset()
        cash = _field(asset, "m_dCash", "cash", "available", default=0.0)
        port = Portfolio(initial_cash=float(cash), cash=float(cash))
        for pos in self.broker.query_positions():
            volume = _field(pos, "m_nVolume", "volume", "shares", default=0)
            symbol = _field(pos, "stock_code", "code", "symbol", default=None)
            volume = float(volume or 0)
            if not symbol or volume <= 0:
                continue
            price = last_prices.get(str(symbol), float(_field(pos, "m_dLastPrice", "last_price", default=0.0) or 0.0))
            port.positions[str(symbol)] = Position(
                symbol=str(symbol),
                shares=volume,
                available=float(_field(pos, "m_nCanUseVolume", "can_use_volume", default=volume) or volume),
                avg_cost=float(_field(pos, "m_dOpenPrice", "open_price", "avg_cost", default=price) or price),
                last_price=price,
            )
        return port

    # ------------------------------------------------------------------ 订单翻译
    def _translate(self, order: Order, last_prices: dict[str, float], execute: bool) -> IntendedOrder:
        qty = int(abs(order.qty))
        side = order.side
        if side == "buy":
            qty = (qty // _ROUND_LOT) * _ROUND_LOT  # 买入整手向下取整
        if qty <= 0:
            return IntendedOrder(order.symbol, side, 0, None, note="LOT_TOO_SMALL")
        price: float | None = None
        if self.cfg.limit_pct is not None:
            ref = last_prices.get(order.symbol)
            if ref is None:
                return IntendedOrder(order.symbol, side, qty, None, note="NO_REF_PRICE")
            price = round(ref * (1 + self.cfg.limit_pct) if side == "buy" else ref * (1 - self.cfg.limit_pct), 3)
        intended = IntendedOrder(order.symbol, side, qty, price)
        if not execute:
            intended.note = "dry-run（--execute 才会真实下单）"
            return intended
        if self.broker is None:
            intended.note = "NO_BROKER"
            return intended
        receipt = self.broker.submit_order(
            order.symbol,
            side,
            qty,
            price=price,
            strategy_name=self.cfg.name,
            order_remark=f"{order.source} {order.created_at}",
        )
        intended.submitted = bool(receipt.get("accepted"))
        intended.order_id = receipt.get("order_id")
        return intended

    # ------------------------------------------------------------------ 产物
    def _save_summary(self, summary: dict[str, Any]) -> None:
        from solidrock.config import get_settings

        out_dir = get_settings().resolved_data_dir() / "live" / "sessions"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = pd.Timestamp(summary["now"]).strftime("%Y%m%d")
        path = out_dir / f"{stamp}_{summary['phase']}.json"
        payload = {"ts": datetime.now(timezone.utc).isoformat(), **summary}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def run_loop(
    session: LiveSession,
    *,
    open_time: str = "09:25",
    trigger_times: list[str] | None = None,
    close_time: str = "15:05",
    execute: bool = False,
    notify: bool = True,
) -> None:
    """常驻调度：交易日自动推进 open → 各触发点 signal → close（盘后通知）.

    仅在交易日运行（对照本地交易日历）；每个时点每日最多执行一次；
    启动时已过的时点会立即补跑一次；Ctrl-C 退出。
    """
    from solidrock.notify import send_webhook

    triggers = sorted(trigger_times or ["14:50"])
    cal = TradingCalendar(session.store)
    fired: dict[str, set[str]] = {}
    error_log = session.store.root / "live" / "session_errors.log"
    while True:
        now = pd.Timestamp.now()
        today = now.strftime("%Y-%m-%d")
        fired.setdefault(today, set())
        if not cal.is_trading_day(today):
            _sleep(60)
            continue
        for when, phase in [
            (open_time, "open"),
            *[(t, "signal") for t in triggers],
            (close_time, "close"),
        ]:
            if when in fired[today] or now.strftime("%H:%M") < when:
                continue
            fired[today].add(when)
            try:
                summary = session.run_phase(phase, now, execute=execute)
                if phase == "close" and notify:
                    n_orders = len(summary["orders"])
                    send_webhook(f"[{session.cfg.name}] 盘后摘要：意图订单 {n_orders} 笔（详见 sessions 产物）")
            except Exception as exc:  # 常驻模式单阶段失败不退出，记录后继续
                error_log.parent.mkdir(parents=True, exist_ok=True)
                with error_log.open("a", encoding="utf-8") as fh:
                    fh.write(f"{now.isoformat()} phase={phase} error={exc!r}\n")
        _sleep(20)


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _field(obj: dict[str, Any], *names: str, default: Any = None) -> Any:
    """防御式字段读取（QMT 返回对象字段以实际为准）。"""
    for name in names:
        value = obj.get(name)
        if value is not None:
            return value
    return default
