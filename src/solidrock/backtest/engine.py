"""事件驱动回测引擎（bar 级、日频）.

每个交易日的处理顺序（``next_open`` 默认模式）::

    1. 公司行为调整（复权因子变化 → 持仓价值守恒调整）
    2. T+1 解锁（昨日买入今日可卖）
    3. 撮合昨日信号（今日开盘价、涨跌停/整手/资金约束）
    4. 更新收盘价 → 逐日盯市（净值曲线）
    5. 回撤熔断检查（触发则次日清仓）
    6. 策略钩子：on_market_open（每交易日首 bar）→ on_signal（每 bar，
       分钟频可配 trigger_times 定时触发）→ on_market_close（每交易日末 bar）；
       钩子看到含当日的数据，产出的订单明日撮合

``same_close`` 模式把第 3、6 步合并到当日收盘，供快速研究（报告会标注）。

设计约定：
- 引擎运行在 **raw 价格空间**（真实货币），费用/涨跌停/资金约束全部正确口径；
- 订单只对下一根 bar 有效，停牌或拒单即过期（不做复杂委托生命周期）；
- 全程无随机性：同一数据快照 + 同一配置 ⇒ 结果完全一致。
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pandas as pd

from solidrock.agent.errors import ErrorCode, err
from solidrock.backtest.config import BacktestConfig
from solidrock.backtest.context import Context
from solidrock.backtest.costs import AShareCostModel, FuturesCostModel
from solidrock.backtest.matching import ExecutionSimulator, Order
from solidrock.backtest.portfolio import Portfolio
from solidrock.data.calendar import TradingCalendar
from solidrock.data.futures import contract_expiry, get_contract_spec
from solidrock.data.symbols import parse_symbol, validate_symbols
from solidrock.risk.checks import DrawdownHalt, PositionWeightCap
from solidrock.strategy.base import Strategy

if TYPE_CHECKING:
    from pathlib import Path

    from solidrock.data.store import DataStore

_TRADING_DAYS_PER_YEAR = 252


@dataclass
class EngineState:
    """引擎内部可变状态（Context 持有只读引用）."""

    dates: list[pd.Timestamp]
    panel: dict[str, pd.DataFrame]
    now: pd.Timestamp = None  # type: ignore[assignment]
    now_idx: int = -1
    params: dict = field(default_factory=dict)
    portfolio: Portfolio = None  # type: ignore[assignment]
    last_prices: dict[str, float] = field(default_factory=dict)
    pending: list[Order] = field(default_factory=list)
    rejections: list[dict] = field(default_factory=list)
    halted: bool = False

    def queue_order(self, order: Order) -> None:
        if self.halted and order.source == "strategy":
            self.rejections.append(
                {"date": self.now, "symbol": order.symbol, "side": order.side, "qty": order.qty, "code": "HALTED"}
            )
            return
        self.pending.append(order)


@dataclass
class BacktestResult:
    """回测结果."""

    run_id: str
    strategy_name: str
    config: BacktestConfig
    nav: pd.DataFrame  # date, total, cash, market_value
    trades: pd.DataFrame
    rejections: pd.DataFrame
    corporate_actions: pd.DataFrame
    metrics: dict
    benchmark: pd.Series | None
    strategy_logs: list[str]
    data_snapshot: str | None
    artifacts_dir: Path | None
    final_positions: dict[str, dict] = None  # type: ignore[assignment]  # {symbol: {shares, available, avg_cost, last_price}}
    final_pending: list[dict] = None  # type: ignore[assignment]  # 未执行订单（carry_pending 模式）
    final_cash: float = 0.0
    final_factors: dict[str, float] = None  # type: ignore[assignment]  # 复权因子游标（模拟盘续用）
    final_halted: bool = False  # 熔断状态（模拟盘续跑必须携带）
    final_halt_peak: float | None = None  # 净值峰值（熔断判定的基准）

    def summary(self) -> dict:
        """关键指标摘要（供 CLI/MCP 打印）。"""
        return {
            "run_id": self.run_id,
            "strategy": self.strategy_name,
            "metrics": self.metrics,
            "data_snapshot": self.data_snapshot,
            "artifacts_dir": str(self.artifacts_dir) if self.artifacts_dir else None,
        }


class BacktestEngine:
    """回测引擎.

    用法::

        engine = BacktestEngine(DualMA, BacktestConfig(start="2024-01-01", end="2025-12-31"), store)
        result = engine.run()
        result.metrics["sharpe"]
    """

    def __init__(
        self,
        strategy: Strategy | type[Strategy],
        config: BacktestConfig,
        store: DataStore,
        *,
        initial_portfolio: Portfolio | None = None,
        initial_pending: list[Order] | None = None,
        initial_last_factors: dict[str, float] | None = None,
        initial_halted: bool = False,
        initial_halt_peak: float | None = None,
    ) -> None:
        """``initial_portfolio``/``initial_pending``/``initial_last_factors``：
        模拟盘增量运行时注入的持久化状态（见 backtest/paper.py）；
        回测场景留空即可。

        ``initial_halted``/``initial_halt_peak``：熔断状态与历史净值峰值。
        模拟盘跨日续跑必须携带，否则每次重启都会重置熔断（``DrawdownHalt`` 是
        单向熔断，重置等于自动恢复了不该恢复的状态），且回撤峰值从头算起。
        """
        self.config = config
        self.store = store
        self._strategy = strategy
        self._initial_portfolio = initial_portfolio
        self._initial_pending = initial_pending
        self._initial_last_factors = initial_last_factors
        self._initial_halted = initial_halted
        self._initial_halt_peak = initial_halt_peak

    # ------------------------------------------------------------------ 入口
    def run(self) -> BacktestResult:
        cfg = self.config
        strategy = self._strategy if isinstance(self._strategy, Strategy) else self._strategy()
        if cfg.cost_model is None:
            cfg.cost_model = AShareCostModel()
        if cfg.freq not in ("1d", "1m", "5m"):
            raise err(ErrorCode.PARAM_INVALID, f"freq 仅支持 1d/1m/5m，收到 {cfg.freq!r}")
        if cfg.freq != "1d" and cfg.execution != "next_open":
            raise err(
                ErrorCode.PARAM_INVALID,
                "分钟回测仅支持 next_open 执行模式",
                hint="分钟粒度下 same_close 的前视风险无法通过报告标注弥补",
            )
        self._trade_records: list[dict] = []
        start_ts = pd.Timestamp(cfg.start).normalize()
        # 分钟频率下 end 需包含当日全部日内 bar（归一到零点会把最后一天排除）
        if cfg.freq == "1d":
            end_ts = pd.Timestamp(cfg.end).normalize()
        else:
            end_ts = pd.Timestamp(cfg.end).normalize() + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        if start_ts > end_ts:
            raise err(ErrorCode.PARAM_INVALID, f"回测区间倒置：{start_ts.date()} > {end_ts.date()}")

        if cfg.freq == "1d":
            # --- 日频：交易日历切分（含 warmup） ---
            cal = TradingCalendar(self.store)
            all_days = cal.days()
            mask = (all_days >= start_ts) & (all_days <= end_ts)
            day_index = all_days[mask]
            if len(day_index) == 0:
                raise err(
                    ErrorCode.NO_DATA,
                    f"回测区间 {start_ts.date()}~{end_ts.date()} 内没有交易日",
                    hint="检查区间或先更新交易日历（srq data calendar --update）",
                )
            first_pos = int(all_days.searchsorted(day_index[0]))
            warmup_start = all_days[max(0, first_pos - cfg.warmup_bars)]
            loaded_days = all_days[all_days >= warmup_start]
            loaded_days = loaded_days[loaded_days <= end_ts]
            backtest_ticks = [d for d in loaded_days if d >= day_index[0]]
            load_start = warmup_start
        else:
            # --- 分钟：时钟来自数据本身的 bar 时间戳；warmup 按日历天回看 ---
            interval_min = 1 if cfg.freq == "1m" else 5
            load_start = start_ts - pd.Timedelta(minutes=cfg.warmup_bars * interval_min * 4)
            backtest_ticks = []  # 数据加载后从 panel 时间戳重建
            loaded_days = pd.DatetimeIndex([])

        # --- setup（设置 universe）与数据加载 ---
        if self._initial_portfolio is not None:
            portfolio = self._initial_portfolio
        else:
            portfolio = Portfolio(initial_cash=cfg.initial_cash, cash=cfg.initial_cash)
        state = EngineState(dates=[], panel={}, params=strategy.params, portfolio=portfolio)
        state.pending = list(self._initial_pending or [])
        state.halted = bool(self._initial_halted)  # 续跑时保持熔断（策略单继续被拒）
        ctx = Context(state)
        state.now = start_ts
        strategy.setup(ctx)
        if not ctx.universe:
            raise err(
                ErrorCode.PARAM_INVALID,
                "策略未设置 ctx.universe",
                hint="在 setup() 中设置 ctx.universe = ['510300.SH', ...]",
            )
        universe = [s.value for s in validate_symbols(ctx.universe)]
        benchmark = None
        if cfg.benchmark is not None:
            benchmark = validate_symbols([cfg.benchmark])[0].value
        symbols = sorted(set(universe) | ({benchmark} if benchmark else set()))

        # 期货+分钟：不支持组合（fail-fast，在数据加载前拦截）
        futures_in_universe = [s for s in universe if parse_symbol(s).is_futures]
        if futures_in_universe and cfg.freq != "1d":
            raise err(
                ErrorCode.PARAM_INVALID,
                "期货回测暂不支持分钟频率",
                hint="期货请使用日线（freq='1d'）；分钟线仅供股票研究",
            )

        data = self.store.load_bars(symbols, start=load_start, end=end_ts, freq=cfg.freq)
        panel: dict[str, pd.DataFrame] = {}
        for symbol, part in data.groupby("symbol"):
            frame = part.drop(columns=["symbol"]).set_index("date").sort_index()
            panel[str(symbol)] = frame
        missing = [s for s in universe if s not in panel]
        if missing:
            raise err(
                ErrorCode.NO_DATA,
                f"universe 中 {len(missing)} 个标的无本地数据：{missing[:5]}{'...' if len(missing) > 5 else ''}",
                hint="先执行 srq data update --symbols <符号> --start <回测起点往前 warmup 天>",
                details={"missing": missing},
            )
        state.panel = panel

        if cfg.freq == "1d":
            ticks = list(loaded_days)
        else:
            # 分钟：时钟 = 全部 bar 时间戳并集（跨标的对齐）
            ticks = sorted({ts for frame in panel.values() for ts in frame.index})
        state.dates = ticks

        if cfg.freq == "1d":
            pass  # backtest_ticks 已由日历得出
        else:
            backtest_ticks = [t for t in ticks if start_ts <= t <= end_ts]
            if not backtest_ticks:
                raise err(
                    ErrorCode.NO_DATA,
                    f"回测区间 {start_ts.date()}~{end_ts.date()} 内没有分钟 bar",
                    hint="先执行 srq data update --freq 1m/5m 补充分钟数据",
                )

        # --- 期货：合约规格/乘数/费用模型（品种路由依据） ---
        futures_symbols = [s for s in symbols if parse_symbol(s).is_futures]
        if futures_symbols and cfg.freq != "1d":
            raise err(
                ErrorCode.PARAM_INVALID,
                "期货回测暂不支持分钟频率",
                hint="期货请使用日线（freq='1d'）；分钟线仅供股票研究",
            )
        spec_map = {s: get_contract_spec(s, cfg.futures_spec_overrides) for s in futures_symbols}
        portfolio.multipliers = {s: spec.multiplier for s, spec in spec_map.items()}
        if futures_symbols and cfg.futures_cost_model is None:
            cfg.futures_cost_model = FuturesCostModel()

        # --- 主循环 ---
        simulator = ExecutionSimulator(
            cfg.cost_model or AShareCostModel(),
            mode=cfg.execution,
            limit_ratio_overrides=cfg.limit_ratio_overrides,
            round_lot=cfg.round_lot,
            futures_cost_model=cfg.futures_cost_model,
        )
        weight_cap = PositionWeightCap(cfg.max_position_weight) if cfg.max_position_weight else None
        halt = DrawdownHalt(cfg.drawdown_halt) if cfg.drawdown_halt else None
        if halt is not None and self._initial_halt_peak is not None:
            # 续跑：沿用历史峰值与已触发状态（单向熔断不得因重启而复位）
            halt.peak = float(self._initial_halt_peak)
            halt.halted = self._initial_halted

        nav_rows: list[dict] = []
        last_factor: dict[str, float] = dict(self._initial_last_factors or {})
        corp_actions: list[dict] = []
        opened_today: dict[str, float] = {}  # 期货：当日开仓手数（平今费率拆分）

        tick_idx = {t: i for i, t in enumerate(ticks)}
        cur_date: pd.Timestamp | None = None  # 当前交易日（日内 T+1 解锁只在换日时发生）
        trigger_times = {str(t) for t in (cfg.trigger_times or [])}

        n_ticks = len(backtest_ticks)
        for i, day in enumerate(backtest_ticks):  # 日频=交易日；分钟=bar 时间戳
            state.now = day
            state.now_idx = tick_idx[day]
            today_bars = {s: panel[s].loc[day] for s in panel if day in panel[s].index}

            # 0) 交易日切换（分钟模式在换日时解锁 T+1 与平今计数；日频每 tick 换日）
            tick_date = pd.Timestamp(day).normalize()
            new_day = tick_date != cur_date
            if new_day:
                cur_date = tick_date
                portfolio.release_available()
                opened_today = {}
            day_end = i == n_ticks - 1 or pd.Timestamp(backtest_ticks[i + 1]).normalize() != tick_date
            # 定时触发（仅分钟频）：配置 trigger_times 后 on_signal 只在指定 HH:MM 的 bar 触发
            fire_signal = cfg.freq == "1d" or not trigger_times or pd.Timestamp(day).strftime("%H:%M") in trigger_times

            # 1) 公司行为：复权因子变化 → 持仓调整（主连换月比例复权同样走这里）
            for symbol, bar in today_bars.items():
                factor = bar["adj_factor"]
                if pd.isna(factor):
                    continue
                factor = float(factor)
                prev = last_factor.get(symbol)
                if prev is not None and abs(factor / prev - 1.0) > 1e-9:
                    ratio = factor / prev
                    pos = portfolio.position(symbol)
                    if abs(pos.shares) > 1e-9:
                        before = pos.shares
                        portfolio.apply_corporate_action(symbol, ratio)
                        corp_actions.append({"date": day, "symbol": symbol, "ratio": ratio, "shares_before": before})
                last_factor[symbol] = factor

            # 2.5) 期货到期强平：到期日（含）之后强制离场（仅日频）
            if cfg.freq == "1d":
                self._handle_futures_expiry(state, spec_map, contract_expiry, day, today_bars)

            if cfg.execution == "next_open":
                # 3) 撮合昨日信号（开盘价）
                self._execute_pending(state, simulator, day, today_bars, weight_cap, spec_map, opened_today)
                # 4) 更新收盘价 + 盯市
                self._update_closes(state, today_bars)
                nav = portfolio.total_value(state.last_prices)
                nav_rows.append(self._nav_row(day, nav, portfolio, state))
                # 5) 熔断
                if halt is not None and halt.check(nav, day):
                    state.halted = True
                    self._queue_liquidation(state, day)
                # 6) 策略钩子：盘前计划 → 信号 → 盘后（订单统一进入 pending，按执行模式撮合）
                if new_day:
                    strategy.on_market_open(ctx)
                if fire_signal:
                    strategy.on_signal(ctx)
                if day_end:
                    strategy.on_market_close(ctx)
            else:  # same_close
                self._update_closes(state, today_bars)
                if new_day:
                    strategy.on_market_open(ctx)
                if fire_signal:
                    strategy.on_signal(ctx)
                if day_end:
                    strategy.on_market_close(ctx)
                self._execute_pending(state, simulator, day, today_bars, weight_cap, spec_map, opened_today)
                nav = portfolio.total_value(state.last_prices)
                nav_rows.append(self._nav_row(day, nav, portfolio, state))
                if halt is not None and halt.check(nav, day):
                    state.halted = True
                    self._queue_liquidation(state, day)

        # 收尾：on_stop 产生的订单与未执行订单——回测记为 NO_MORE_BARS，
        # 模拟盘（carry_pending）携带到下一交易时段
        strategy.on_stop(ctx)
        final_pending = list(state.pending)
        if not cfg.carry_pending:
            for order in state.pending:
                state.rejections.append(
                    {
                        "date": state.now,
                        "symbol": order.symbol,
                        "side": order.side,
                        "qty": order.qty,
                        "code": "NO_MORE_BARS",
                    }
                )
        state.pending.clear()

        nav_df = (
            pd.DataFrame(nav_rows).set_index("date")
            if nav_rows
            else pd.DataFrame(columns=["total", "cash", "market_value"])
        )
        trades_df = pd.DataFrame(self._trade_records)
        rejections_df = pd.DataFrame(state.rejections)
        corp_df = pd.DataFrame(corp_actions)
        benchmark_series = self._benchmark_series(
            panel, benchmark, ticks[-1] if ticks else backtest_ticks[0], backtest_ticks[0]
        )

        from solidrock.report.metrics import compute_metrics

        nav_total = nav_df["total"] if not nav_df.empty else pd.Series(dtype="float64")
        if cfg.freq != "1d" and not nav_total.empty:
            # 分钟回测：指标按日重采样（最后 bar 即当日收盘），保证年化口径一致
            nav_daily = nav_total.groupby(nav_total.index.normalize()).last()
            bench_daily = (
                benchmark_series.groupby(benchmark_series.index.normalize()).last()
                if benchmark_series is not None
                else None
            )
            metrics = compute_metrics(nav_daily, bench_daily, trades_df if not trades_df.empty else None)
        else:
            metrics = compute_metrics(
                nav_total,
                benchmark_series,
                trades_df if not trades_df.empty else None,
            )

        result = BacktestResult(
            run_id=f"bt-{pd.Timestamp.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
            strategy_name=type(strategy).__name__,
            config=cfg,
            nav=nav_df,
            trades=trades_df,
            rejections=rejections_df,
            corporate_actions=corp_df,
            metrics=metrics,
            benchmark=benchmark_series,
            strategy_logs=ctx.logs,
            data_snapshot=self.store.snapshot,
            artifacts_dir=None,
            final_positions={
                symbol: {
                    "shares": pos.shares,
                    "available": pos.available,
                    "avg_cost": pos.avg_cost,
                    "last_price": pos.last_price,
                }
                for symbol, pos in portfolio.positions.items()
                if abs(pos.shares) > 1e-9
            },
            final_pending=(
                [{"symbol": o.symbol, "side": o.side, "qty": o.qty, "source": o.source} for o in final_pending]
                if cfg.carry_pending
                else []
            ),
            final_cash=portfolio.cash,
            final_factors=dict(last_factor),
            final_halted=bool(state.halted),
            final_halt_peak=float(halt.peak) if halt is not None and halt.peak != -math.inf else None,
        )
        if cfg.log_experiment:
            self._log_experiment(result)
        return result

    # ------------------------------------------------------------------ 内部
    def _update_closes(self, state: EngineState, today_bars: dict[str, pd.Series]) -> None:
        for symbol, bar in today_bars.items():
            close = bar["close"]
            if not pd.isna(close):
                state.last_prices[symbol] = float(close)

    def _execute_pending(
        self,
        state: EngineState,
        simulator: ExecutionSimulator,
        day: pd.Timestamp,
        today_bars: dict[str, pd.Series],
        weight_cap: PositionWeightCap | None,
        spec_map: dict | None = None,
        opened_today: dict[str, float] | None = None,
    ) -> None:
        pending, state.pending = state.pending, []
        opened_today = opened_today if opened_today is not None else {}
        for order in pending:
            bar = today_bars.get(order.symbol)
            if bar is None:
                state.rejections.append(
                    {"date": day, "symbol": order.symbol, "side": order.side, "qty": order.qty, "code": "SUSPENDED"}
                )
                continue
            spec = (spec_map or {}).get(order.symbol)
            if spec is not None:
                expiry = contract_expiry(order.symbol)
                if order.source == "strategy" and expiry is not None and day >= expiry:
                    state.rejections.append(
                        {
                            "date": day,
                            "symbol": order.symbol,
                            "side": order.side,
                            "qty": order.qty,
                            "code": "EXPIRED",
                        }
                    )
                    continue
                result = simulator.simulate_futures_fill(
                    order,
                    bar,
                    state.portfolio,
                    spec,
                    opened_today=opened_today.get(order.symbol, 0.0),
                    margin_headroom=self._margin_headroom(state, spec_map or {}, order.symbol, bar, simulator),
                )
            else:
                # 仓位权重上限仅约束股票（期货是保证金交易，权重无意义）
                if order.side == "buy" and weight_cap is not None:
                    ref = float(bar["open" if simulator.mode == "next_open" else "close"])
                    total_value = state.portfolio.total_value(state.last_prices)
                    # 已持仓市值参与封顶：否则逐笔加仓每笔都能再买满上限
                    held = abs(state.portfolio.position(order.symbol).shares) * ref
                    capped = weight_cap.cap_qty(order.symbol, order.qty, ref, total_value, current_value=held)
                    if capped < order.qty:
                        state.rejections.append(
                            {
                                "date": day,
                                "symbol": order.symbol,
                                "side": order.side,
                                "qty": order.qty - capped,
                                "code": "WEIGHT_CAP",
                            }
                        )
                        order = Order(order.symbol, order.side, capped, order.created_at, order.source)
                        if order.qty <= 0:
                            continue
                result = simulator.simulate_fill(order, bar, state.portfolio)
            if result.rejected:
                state.rejections.append(
                    {
                        "date": day,
                        "symbol": order.symbol,
                        "side": order.side,
                        "qty": order.qty,
                        "code": result.rejected,
                    }
                )
                continue

            if spec is not None:
                signed_delta = result.filled_qty if order.side == "buy" else -result.filled_qty
                pnl = state.portfolio.apply_fill(order.symbol, signed_delta, result.price, result.fees)
                pos = state.portfolio.position(order.symbol)  # 期货 T+0：可用手数即时同步
                pos.available = pos.shares
                opened_today[order.symbol] = max(
                    opened_today.get(order.symbol, 0.0) + result.open_qty - result.close_today_qty, 0.0
                )
            elif order.side == "buy":
                state.portfolio.buy(
                    order.symbol,
                    result.filled_qty,
                    result.price,
                    result.fees,
                    immediate_available=parse_symbol(order.symbol).is_t0,
                )
                pnl = None
            else:
                trade = state.portfolio.sell(order.symbol, result.filled_qty, result.price, result.fees)
                pnl = trade.pnl
            self._trade_records.append(
                {
                    "date": day,
                    "symbol": order.symbol,
                    "side": order.side,
                    "qty": result.filled_qty,
                    "price": result.price,
                    "value": result.value,
                    "fees": result.fees,
                    "cash_after": state.portfolio.cash,
                    "pnl": pnl,
                    "closing": result.close_qty > 0,
                }
            )

    def _margin_headroom(
        self,
        state: EngineState,
        spec_map: dict,
        symbol: str,
        bar: pd.Series,
        simulator: ExecutionSimulator,
    ) -> float:
        """可用保证金空间 = 权益 - 全组合保证金占用 + 本次平仓预计释放.

        权益 = cash + Σ signed_shares × price × multiplier（现金流为带符号
        全额价值口径，见 Portfolio.apply_fill）。
        """
        portfolio = state.portfolio
        used = 0.0
        for sym in portfolio.position_symbols:
            spec = spec_map.get(sym)
            if spec is None:
                continue
            pos = portfolio.position(sym)
            price = state.last_prices.get(sym, pos.last_price)
            used += abs(pos.shares) * price * spec.multiplier * spec.margin_rate
        free = portfolio.total_value(state.last_prices) - used
        spec = spec_map.get(symbol)
        if spec is not None:
            pos = portfolio.position(symbol)
            ref = float(bar["open" if simulator.mode == "next_open" else "close"])
            free += abs(pos.shares) * ref * spec.multiplier * spec.margin_rate  # 平仓释放
        return free

    def _handle_futures_expiry(
        self,
        state: EngineState,
        spec_map: dict,
        expiry_of,
        day: pd.Timestamp,
        today_bars: dict[str, pd.Series],
    ) -> None:
        """期货到期强平：到期日（含）起强制离场；无 bar 时按最近价直接了结."""
        for symbol in list(state.portfolio.position_symbols):
            spec = spec_map.get(symbol)
            if spec is None:
                continue
            expiry = expiry_of(symbol)
            if expiry is None or day < expiry:
                continue
            pos = state.portfolio.position(symbol)
            signed_delta = -pos.shares  # 全部了结（多头卖出 / 空头买入回补）
            bar = today_bars.get(symbol)
            if bar is not None:
                state.queue_order(
                    Order(
                        symbol=symbol,
                        side="sell" if pos.shares > 0 else "buy",
                        qty=abs(pos.shares),
                        created_at=day,
                        source="expiry",
                    )
                )
                continue
            # 停牌/数据缺失：按最近价直接了结（零费用近似）
            pnl = state.portfolio.apply_fill(symbol, signed_delta, pos.last_price, 0.0)
            self._trade_records.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "side": "sell" if signed_delta > 0 else "buy",
                    "qty": abs(signed_delta),
                    "price": pos.last_price,
                    "value": abs(signed_delta) * pos.last_price * spec.multiplier,
                    "fees": 0.0,
                    "cash_after": state.portfolio.cash,
                    "pnl": pnl,
                    "closing": True,
                }
            )

    def _queue_liquidation(self, state: EngineState, day: pd.Timestamp) -> None:
        for symbol in state.portfolio.position_symbols:
            state.queue_order(
                Order(
                    symbol=symbol,
                    side="sell",
                    qty=state.portfolio.position(symbol).shares,
                    created_at=day,
                    source="risk_liquidate",
                )
            )

    @staticmethod
    def _nav_row(day: pd.Timestamp, nav: float, portfolio: Portfolio, state: EngineState) -> dict:
        market_value = portfolio.market_value(state.last_prices)
        return {"date": day, "total": nav, "cash": portfolio.cash, "market_value": market_value}

    def _benchmark_series(
        self,
        panel: dict[str, pd.DataFrame],
        benchmark: str | None,
        last_loaded_day: pd.Timestamp,
        first_backtest_day: pd.Timestamp,
    ) -> pd.Series | None:
        if benchmark is None or benchmark not in panel:
            return None
        frame = panel[benchmark]
        # 用后复权收盘（因子=1 的指数即原始收盘），以回测起始日前最后一个收盘为基准 1.0；
        # 无更早数据（warmup 不足）时退化为区间首日
        hfq_close = (frame["close"] * frame["adj_factor"].fillna(1.0)).dropna()
        base_idx = hfq_close.index[hfq_close.index < first_backtest_day]
        window = hfq_close[(hfq_close.index >= first_backtest_day) & (hfq_close.index <= last_loaded_day)]
        if window.empty:
            return None
        base = hfq_close.loc[base_idx[-1]] if not base_idx.empty else window.iloc[0]
        return window / base

    def _log_experiment(self, result: BacktestResult) -> None:
        from solidrock.experiments.tracker import ExperimentTracker

        artifacts_dir = self.store.root / "runs" / result.run_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        from solidrock.report.json_report import write_result_json
        from solidrock.report.markdown import write_report_markdown

        write_report_markdown(result, artifacts_dir / "report.md")
        write_result_json(result, artifacts_dir / "result.json")
        result.trades.to_csv(artifacts_dir / "trades.csv", index=False)
        result.nav.to_csv(artifacts_dir / "nav.csv")
        result.artifacts_dir = artifacts_dir
        try:
            from solidrock.report.html import write_report_html

            write_report_html(result, artifacts_dir / "report.html")
        except ImportError:
            pass  # plotly 未安装（extras report）：仅保留 Markdown/JSON 产物

        tracker = ExperimentTracker(self.store.root / "experiments.db")
        tracker.log_run(
            kind="backtest",
            name=result.config.name or result.strategy_name,
            config=result.config.to_dict(),
            metrics=result.metrics,
            artifacts_dir=str(artifacts_dir),
            data_snapshot=result.data_snapshot,
            notes=result.config.notes,
            run_id=result.run_id,
        )
