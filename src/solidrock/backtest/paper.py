"""模拟盘：状态持久化的日频 paper trading.

工作方式（增量、复用事件引擎全部规则）：

1. 首次 ``run``：以 ``config.start`` 为起点，用当前数据跑到最新交易日，
   组合状态（现金/持仓/未执行订单/复权因子游标）持久化到
   ``{data_dir}/paper/{name}/state.json``；
2. 之后每个交易日再 ``run``：从上次停留处继续——昨日收盘产生的订单在
   今日开盘撮合、策略在今日收盘产出新订单、净值与持仓落盘；
3. 建议每交易日收盘后运行一次（可由系统计划任务调度），跳过的交易日的
   公司行为（分红除权）不会被追溯调整。

与回测的差别：``carry_pending=True``（未执行订单携带到下一时段）、
``log_experiment=False``（运行摘要记录在 paper 目录而非实验库）、
仅支持 ``next_open`` 执行模式（模拟盘语义就是防前视）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from solidrock.agent.errors import ErrorCode, err
from solidrock.backtest.config import BacktestConfig
from solidrock.backtest.engine import BacktestEngine
from solidrock.backtest.matching import Order
from solidrock.backtest.portfolio import Portfolio, Position
from solidrock.data.calendar import TradingCalendar
from solidrock.data.store import DataStore
from solidrock.strategy.loader import load_strategy_class

_STATE_VERSION = 1


@dataclass
class PaperState:
    """模拟盘持久化状态."""

    version: int
    name: str
    strategy_file: str
    params: dict
    start: str
    cash: float
    initial_cash: float
    positions: dict[str, dict[str, float]]
    pending: list[dict[str, Any]]
    last_factors: dict[str, float]
    last_date: str | None  # 最后已处理的交易日
    run_count: int
    created_at: str
    updated_at: str
    multipliers: dict[str, float] = field(default_factory=dict)


class PaperTrader:
    """模拟盘交易器：增量运行、状态持久化.

    ``params`` 首次运行时确定并持久化；之后每次运行沿用（改参数请换名字新建）。
    """

    def __init__(
        self,
        strategy_file: str,
        config: BacktestConfig,
        store: DataStore,
        *,
        name: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        if config.execution != "next_open":
            raise err(
                ErrorCode.PARAM_INVALID,
                "模拟盘仅支持 next_open 执行模式",
                hint="模拟盘的语义就是防前视：今日收盘出信号、明日开盘成交",
            )
        self.strategy_file = str(strategy_file)
        self.config = config
        self.store = store
        self.name = name
        self.params = dict(params or {})
        self.state_dir = store.root / "paper" / name
        self.state_path = self.state_dir / "state.json"
        self.history_path = self.state_dir / "history.jsonl"

    # ------------------------------------------------------------------ 运行
    def run(self, *, end: str | None = None) -> dict[str, Any]:
        """处理到 ``end``（默认今天）为止的新交易日，返回本次运行摘要."""
        state = self._load_state()
        first_run = state is None
        if state is None:
            state = self._init_state()

        start_for_engine = self._next_start(state)
        if self._no_new_days(start_for_engine, end):
            return {
                "status": "ok",
                "ran": False,
                "name": self.name,
                "last_date": state.last_date,
                "note": "没有新的交易日（周末或今日已运行）",
            }

        cfg = self._paper_config(start_for_engine, end)
        strategy_cls = load_strategy_class(state.strategy_file)
        portfolio, pending, last_factors = self._restore(state)
        engine = BacktestEngine(
            strategy_cls(**state.params),
            cfg,
            self.store,
            initial_portfolio=portfolio,
            initial_pending=pending,
            initial_last_factors=last_factors,
        )
        result = engine.run()

        self._save_state(state, result, cfg)
        summary = self._summary(state, result, first_run)
        self._append_history(summary)
        return summary

    def status(self) -> dict[str, Any]:
        """当前持久化状态摘要。"""
        state = self._load_state()
        if state is None:
            raise err(
                ErrorCode.NO_DATA,
                f"模拟盘 {self.name!r} 尚未初始化",
                hint=f"先运行 srq paper run --name {self.name}",
            )
        return {
            "name": state.name,
            "strategy": state.strategy_file,
            "params": state.params,
            "cash": state.cash,
            "initial_cash": state.initial_cash,
            "positions": state.positions,
            "pending_orders": state.pending,
            "last_date": state.last_date,
            "run_count": state.run_count,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
        }

    # ------------------------------------------------------------------ 内部
    def _state_dir_ensure(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _init_state(self) -> PaperState:
        self._state_dir_ensure()
        now = datetime.now(timezone.utc).isoformat()
        return PaperState(
            version=_STATE_VERSION,
            name=self.name,
            strategy_file=self.strategy_file,
            params=dict(self.params),
            start=str(pd.Timestamp(self.config.start).date()),
            cash=self.config.initial_cash,
            initial_cash=self.config.initial_cash,
            positions={},
            pending=[],
            last_factors={},
            last_date=None,
            run_count=0,
            created_at=now,
            updated_at=now,
        )

    def _load_state(self) -> PaperState | None:
        if not self.state_path.exists():
            return None
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        return PaperState(
            version=raw["version"],
            name=raw["name"],
            strategy_file=raw["strategy_file"],
            params=raw["params"],
            start=raw["start"],
            cash=raw["cash"],
            initial_cash=raw["initial_cash"],
            positions=raw["positions"],
            pending=raw["pending"],
            last_factors=raw.get("last_factors", {}),
            last_date=raw["last_date"],
            run_count=raw["run_count"],
            created_at=raw["created_at"],
            updated_at=raw["updated_at"],
            multipliers=raw.get("multipliers", {}),
        )

    def _save_state(self, state: PaperState, result, cfg: BacktestConfig) -> None:
        state.cash = result.final_cash
        state.positions = dict(result.final_positions)
        state.pending = list(result.final_pending)
        state.last_factors = dict(result.final_factors)
        state.multipliers = (
            dict(result.final_multipliers) if hasattr(result, "final_multipliers") else state.multipliers
        )
        if result.nav is not None and not result.nav.empty:
            state.last_date = str(result.nav.index[-1].date())
        state.run_count += 1
        state.updated_at = datetime.now(timezone.utc).isoformat()
        self._state_dir_ensure()
        self.state_path.write_text(
            json.dumps(state.__dict__, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def _restore(self, state: PaperState) -> tuple[Portfolio, list[Order], dict[str, float]]:
        """从持久化状态重建组合、未执行订单与复权因子游标."""
        p = Portfolio(initial_cash=state.initial_cash, cash=state.cash)
        p.multipliers = dict(state.multipliers)
        for symbol, d in state.positions.items():
            pos = p.positions.setdefault(symbol, Position(symbol=symbol))
            pos.shares = d["shares"]
            pos.available = d.get("available", d["shares"])
            pos.avg_cost = d["avg_cost"]
            pos.last_price = d["last_price"]
        pending = [
            Order(symbol=o["symbol"], side=o["side"], qty=o["qty"], source=o.get("source", "strategy"))
            for o in state.pending
        ]
        return p, pending, dict(state.last_factors)

    def _next_start(self, state: PaperState) -> str:
        if state.last_date is None:
            return state.start
        return (pd.Timestamp(state.last_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    def _no_new_days(self, start: str, end: str | None) -> bool:
        cal = TradingCalendar(self.store)
        end_ts = pd.Timestamp(end) if end is not None else pd.Timestamp.now().normalize()
        days = cal.days(start, end_ts)
        return len(days) == 0

    def _paper_config(self, start: str, end: str | None) -> BacktestConfig:
        cfg = self.config
        return BacktestConfig(
            start=start,
            end=end or pd.Timestamp.now().strftime("%Y-%m-%d"),
            benchmark=cfg.benchmark,
            execution="next_open",
            initial_cash=cfg.initial_cash,
            max_position_weight=cfg.max_position_weight,
            drawdown_halt=cfg.drawdown_halt,
            limit_ratio_overrides=cfg.limit_ratio_overrides,
            warmup_bars=cfg.warmup_bars,
            round_lot=cfg.round_lot,
            cost_model=cfg.cost_model,
            futures_cost_model=cfg.futures_cost_model,
            futures_spec_overrides=cfg.futures_spec_overrides,
            name=cfg.name or f"paper-{self.name}",
            notes=cfg.notes,
            log_experiment=False,  # 模拟盘的运行摘要记录在 paper 目录
            carry_pending=True,
        )

    def _summary(self, state: PaperState, result, first_run: bool) -> dict[str, Any]:
        return {
            "status": "ok",
            "ran": True,
            "name": self.name,
            "first_run": first_run,
            "run_id": result.run_id,
            "ran_days": len(result.nav),
            "last_date": state.last_date,
            "cash": state.cash,
            "positions": state.positions,
            "pending_orders": state.pending,
            "metrics": result.metrics,
            "trades": result.trades.to_dict("records") if not result.trades.empty else [],
            "note": "模拟盘每日运行一次；净值与持仓见 " + str(self.state_dir),
        }

    def _append_history(self, summary: dict[str, Any]) -> None:
        self._state_dir_ensure()
        line = json.dumps(
            {
                "run_id": summary["run_id"],
                "last_date": summary["last_date"],
                "cash": summary["cash"],
                "positions": summary["positions"],
                "trades": summary["trades"],
                "metrics": summary["metrics"],
            },
            ensure_ascii=False,
            default=str,
        )
        with self.history_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
