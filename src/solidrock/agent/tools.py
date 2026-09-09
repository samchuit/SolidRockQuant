"""MCP 工具实现：统一信封 + 领域层薄封装.

信封格式（见 docs/design.md §8.2）::

    {
      "status": "ok | error",
      "data": {...紧凑摘要...},          # error 时为 None
      "artifacts": ["/abs/path/..."],    # 产物文件路径
      "error": {"code", "message", "hint"},  # 仅失败时
      "next_suggested_tools": ["tool:arg", ...]
    }

设计原则：
- **token 经济**：返回紧凑摘要 + 文件路径引用，绝不把大 DataFrame 塞给模型；
- **自解释报错**：错误必带可执行 hint；
- **非负浮点净化**：NaN/Inf 在序列化前转为 None（严格 JSON 兼容）。

工具函数（``tool_*``）与 MCP 装置（mcp_server.py）解耦，可直接单测。
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

import pandas as pd

from solidrock.agent.errors import ErrorCode, SolidRockError, err
from solidrock.backtest import BacktestConfig, BacktestEngine
from solidrock.config import get_settings
from solidrock.data.calendar import TradingCalendar
from solidrock.data.sources import create_source, list_sources
from solidrock.data.store import DataStore
from solidrock.data.symbols import validate_symbols
from solidrock.experiments.tracker import ExperimentTracker
from solidrock.strategy.loader import load_strategy_class


# ---------------------------------------------------------------------- 信封
def json_safe(obj: Any) -> Any:
    """递归净化：NaN/Inf → None（严格 JSON 兼容），Timestamp/date → ISO 字符串。"""
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if isinstance(obj, (int,)):
        return obj
    if isinstance(obj, (pd.Timestamp,)):
        return None if pd.isna(obj) else obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj


def ok(data: Any, *, artifacts: list[str] | None = None, next_tools: list[str] | None = None) -> dict:
    return {
        "status": "ok",
        "data": json_safe(data),
        "artifacts": artifacts or [],
        "error": None,  # 信封 shape 恒定：Agent 可无脑读同一组键
        "next_suggested_tools": next_tools or [],
    }


def fail(exc: SolidRockError, *, next_tools: list[str] | None = None) -> dict:
    return {
        "status": "error",
        "data": None,
        "artifacts": [],
        "error": exc.to_dict(),
        "next_suggested_tools": next_tools or [],
    }


def dumps(envelope: dict) -> str:
    """信封 → JSON 字符串（MCP 返回值）。"""
    return json.dumps(envelope, ensure_ascii=False, indent=2, allow_nan=False, default=str)


def _store() -> DataStore:
    return DataStore(get_settings().resolved_data_dir())


def _tracker() -> ExperimentTracker:
    return ExperimentTracker(get_settings().resolved_data_dir() / "experiments.db")


def _run_tool(fn, **kwargs: Any) -> str:
    """统一入口：捕获 SolidRockError 与未预期异常，永远返回信封 JSON。"""
    try:
        return dumps(ok(fn(**kwargs)))
    except SolidRockError as exc:
        return dumps(fail(exc))
    except Exception as exc:
        return dumps(
            fail(
                err(
                    ErrorCode.INTERNAL_ERROR,
                    f"工具执行失败：{type(exc).__name__}: {exc}",
                    hint="携带完整参数复现并提交 issue；可先用更小的参数范围重试",
                )
            )
        )


# ---------------------------------------------------------------------- 数据类
def _data_overview() -> dict:
    store = _store()
    bars: dict[str, Any] = {}
    for freq in ("1d", "1m", "5m"):
        symbols = store.symbols(freq)
        if not symbols:
            continue
        frames = [store.load_bars(s, freq=freq, columns=["date"]) for s in symbols[:200]]
        all_dates = pd.concat([f["date"] for f in frames if not f.empty])
        bars[freq] = {
            "n_symbols": len(symbols),
            "n_rows": int(store.row_count(freq=freq)),
            "first": str(all_dates.min().date()) if not all_dates.empty else None,
            "last": str(all_dates.max().date()) if not all_dates.empty else None,
        }
    cal = store.load_calendar()
    cal_info = (
        {"n_days": len(cal), "first": str(cal["date"].min().date()), "last": str(cal["date"].max().date())}
        if cal is not None and not cal.empty
        else None
    )
    db = get_settings().resolved_data_dir() / "experiments.db"
    n_experiments = len(ExperimentTracker(db).list_runs(limit=1000)) if db.exists() else 0
    return {
        "data_dir": str(store.root),
        "bars": bars,
        "calendar": cal_info,
        "snapshots": store.list_snapshots(),
        "n_experiments": n_experiments,
    }


def _data_health(symbols: list[str] | None = None, jump_threshold: float = 0.2) -> dict:
    from solidrock.data.quality import check_store

    report = check_store(_store(), symbols, jump_threshold=jump_threshold)
    payload = report.to_dict()
    payload["note"] = "大幅波动可能来自换月（主连）或真实行情；因子缺失会使前复权不可用"
    return payload


def _search_instruments(query: str, limit: int = 20, source: str | None = None) -> dict:
    store = _store()
    found = store.search_instruments(query, limit=limit)
    if found.empty:
        # 缓存未命中 → 尝试从数据源拉取标的列表并缓存
        src = create_source(source or get_settings().default_source)
        instruments = src.fetch_instruments()
        if instruments.empty:
            raise err(
                ErrorCode.NO_DATA,
                f"数据源 {src.name} 未返回标的列表",
                hint="检查网络后重试",
            )
        store.save_instruments(instruments)
        found = store.search_instruments(query, limit=limit)
    return {"query": query, "matches": found.to_dict("records"), "n_matches": len(found)}


def _fetch_bars(
    symbols: list[str] | str,
    start: str | None = None,
    end: str | None = None,
    source: str | None = None,
    with_adj_factor: bool = True,
    freq: str = "1d",
) -> dict:
    symbol_list = [symbols] if isinstance(symbols, str) else list(symbols)
    validate_symbols(symbol_list)
    src = create_source(source or get_settings().default_source)
    store = _store()
    t0 = time.perf_counter()
    df = src.fetch_bars(symbol_list, start=start, end=end, freq=freq, with_adj_factor=with_adj_factor)
    if df.empty:
        raise err(
            ErrorCode.NO_DATA,
            f"数据源 {src.name} 未返回任何数据" + (f"（区间 {start} ~ {end}）" if start or end else ""),
            hint="检查符号与日期区间；首次拉取建议显式给 start",
        )
    totals = store.update_bars(df, freq=freq, source=src.name)
    summary = {}
    for symbol in sorted(totals):
        part = df[df["symbol"] == symbol]
        summary[symbol] = {
            "rows_in_store": totals[symbol],
            "first": str(part["date"].min().date()),
            "last": str(part["date"].max().date()),
        }
    return {
        "source": src.name,
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "symbols": summary,
    }


def _trading_calendar(
    start: str | None = None,
    end: str | None = None,
    update: bool = False,
    source: str | None = None,
) -> dict:
    store = _store()
    cal = TradingCalendar(store)
    updated_days: int | None = None
    if update:
        src = create_source(source or get_settings().default_source)
        updated_days = cal.update(src)
    days = cal.days(start, end) if (start or end) else cal._require_loaded()[:0]
    info = cal.coverage()
    return {
        "updated": update,
        "updated_days": updated_days,
        "coverage": {"first": str(info[0].date()), "last": str(info[1].date())} if info else None,
        "n_days_total": len(cal._require_loaded()),
        "n_days_in_range": len(days),
        "sample_days": [str(d.date()) for d in days[:5]],
    }


# ---------------------------------------------------------------------- 回测类
def _validate_strategy(strategy_file: str) -> dict:
    from solidrock.agent.validation import validate_strategy_file

    report = validate_strategy_file(strategy_file)
    errors = [i for i in report["issues"] if i["severity"] == "error"]
    if errors:
        raise err(
            ErrorCode.STRATEGY_INVALID,
            f"策略静态检查未通过（{len(errors)} 个 error）",
            hint="按 issues 逐条修复后重试；第一条 error 通常最重要",
            details={"issues": report["issues"], "passed": report["passed"]},
        )
    return report


def _run_backtest(
    strategy_file: str,
    start: str,
    end: str,
    params: dict[str, Any] | None = None,
    benchmark: str | None = "000300.SH",
    execution: str = "next_open",
    initial_cash: float = 1_000_000.0,
    name: str | None = None,
    notes: str | None = None,
) -> dict:
    # 回测前先做静态校验（前视/幻觉 API 是 LLM 策略的常见病）
    _validate_strategy(strategy_file)
    strategy_cls = load_strategy_class(strategy_file)
    cfg = BacktestConfig(
        start=start,
        end=end,
        benchmark=benchmark,
        execution=execution,  # type: ignore[arg-type]
        initial_cash=initial_cash,
        name=name,
        notes=notes,
    )
    result = BacktestEngine(strategy_cls(**(params or {})), cfg, _store()).run()
    rejection_counts = result.rejections["code"].value_counts().to_dict() if not result.rejections.empty else {}
    artifacts_dir = result.artifacts_dir
    artifacts = (
        [str(artifacts_dir / f) for f in ("report.md", "result.json", "trades.csv", "nav.csv")]
        if artifacts_dir is not None
        else []
    )
    return {
        "run_id": result.run_id,
        "strategy": result.strategy_name,
        "metrics": result.metrics,
        "rejection_counts": rejection_counts,
        "data_snapshot": result.data_snapshot,
        "final_positions": result.final_positions,
        "artifacts": artifacts,
        "note": "完整净值/交易明细见 artifacts；对比历史实验用 compare_experiments",
    }


def _run_vectorized_backtest(
    factor_file: str,
    universe: list[str],
    start: str,
    end: str,
    params: dict[str, Any] | None = None,
    top: float = 0.2,
    bottom: float = 0.2,
    fee_rate: float = 1.5e-4,
    warmup_bars: int = 250,
    name: str | None = None,
    notes: str | None = None,
) -> dict:
    """向量化因子筛选：因子值 → 多空权重 → 净值（秒级，用于快速迭代）."""
    import uuid

    from solidrock.backtest.vectorized import vectorized_backtest, weights_from_factor
    from solidrock.data.calendar import TradingCalendar
    from solidrock.experiments.tracker import ExperimentTracker
    from solidrock.factors import FactorData, load_factor_class

    factor_cls = load_factor_class(factor_file)
    factor = factor_cls(**(params or {}))
    store = _store()
    universe_list = [s.value for s in validate_symbols(universe)]
    cal = TradingCalendar(store)
    all_days = cal.days()
    start_ts, end_ts = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    eval_days = all_days[(all_days >= start_ts) & (all_days <= end_ts)]
    if len(eval_days) == 0:
        raise err(ErrorCode.NO_DATA, f"区间 {start}~{end} 内没有交易日", hint="检查区间或更新日历")
    first_pos = int(all_days.searchsorted(eval_days[0]))
    load_start = all_days[max(0, first_pos - max(warmup_bars, factor.lookback))]
    bars = store.load_bars(universe_list, start=load_start, end=end_ts)
    if bars.empty:
        raise err(
            ErrorCode.NO_DATA,
            "universe 中没有本地数据",
            hint="先执行 fetch_bars 更新数据",
        )
    data = FactorData.from_bars(bars)
    values = factor.compute(data).reindex(pd.DatetimeIndex(eval_days))
    close = data.hfq_close().reindex(pd.DatetimeIndex(eval_days))
    weights = weights_from_factor(values, top=top, bottom=bottom)
    result = vectorized_backtest(weights, close, fee_rate=fee_rate)
    result.metrics["annual_turnover"] = float(result.turnover.mean() * 252)

    run_id = f"bt-{pd.Timestamp.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    artifacts_dir = store.root / "runs" / run_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = {
        "start": str(start_ts.date()),
        "end": str(end_ts.date()),
        "universe": universe_list,
        "top": top,
        "bottom": bottom,
        "fee_rate": fee_rate,
        "data_snapshot": store.snapshot,
    }
    payload: dict[str, Any] = {
        "run_id": run_id,
        "mode": "vectorized（逐日再平衡、收盘成交、单一费率，仅用于相对比较）",
        "factor": factor.name,
        "params": factor.params,
        "config": config,
        "metrics": result.metrics,
    }
    (artifacts_dir / "result.json").write_text(
        json.dumps(json_safe(payload), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    result.nav.rename("nav").to_csv(artifacts_dir / "nav.csv")
    weights.to_parquet(artifacts_dir / "weights.parquet")
    tracker = ExperimentTracker(store.root / "experiments.db")
    tracker.log_run(
        kind="backtest",
        name=name or f"[screen] {factor.name}",
        config={**config, "mode": "vectorized", "factor": factor.name, "factor_params": factor.params},
        metrics=result.metrics,
        artifacts_dir=str(artifacts_dir),
        data_snapshot=store.snapshot,
        notes=notes,
        run_id=run_id,
    )
    return {
        "run_id": run_id,
        "factor": factor.name,
        "metrics": result.metrics,
        "artifacts": [str(artifacts_dir / f) for f in ("result.json", "nav.csv", "weights.parquet")],
        "data_snapshot": store.snapshot,
        "note": "向量化口径用于相对比较；最终结论请用 run_backtest 事件引擎复核",
    }


def _run_sandboxed_backtest(
    strategy_file: str,
    start: str,
    end: str,
    params: dict[str, Any] | None = None,
    benchmark: str | None = "000300.SH",
    timeout: float = 300.0,
    name: str | None = None,
) -> dict:
    """沙箱回测：子进程隔离执行，坏策略（死循环/崩溃）不影响宿主。"""
    from solidrock.agent.sandbox import run_sandboxed_backtest as _sandbox_run

    return _sandbox_run(
        strategy_file,
        start=start,
        end=end,
        data_dir=str(get_settings().resolved_data_dir()),
        params=params,
        benchmark=benchmark,
        execution="next_open",
        name=name,
        timeout=timeout,
    )


def _paper_session(
    strategy_file: str,
    name: str,
    start: str | None = None,
    end: str | None = None,
    params: dict[str, Any] | None = None,
    initial_cash: float = 1_000_000.0,
) -> dict:
    from solidrock.backtest.paper import PaperTrader

    cfg = BacktestConfig(
        start=start or "1990-01-01",
        end=end or "2099-12-31",
        benchmark=None,
        initial_cash=initial_cash,
        log_experiment=False,
    )
    trader = PaperTrader(strategy_file, cfg, _store(), name=name, params=params)
    summary = trader.run(end=end)
    summary["note"] = "模拟盘建议每交易日收盘后运行一次；跳过的交易日的公司行为不追溯调整"
    return summary


def _paper_status(name: str) -> dict:
    from solidrock.backtest.paper import PaperTrader

    cfg = BacktestConfig(start="1990-01-01", end="2099-12-31", benchmark=None)
    return PaperTrader("unknown", cfg, _store(), name=name).status()


def _list_experiments(kind: str | None = None, limit: int = 20) -> dict:
    runs = _tracker().list_runs(kind=kind, limit=limit)
    compact = [
        {
            "run_id": r["id"],
            "name": r["name"],
            "created_at": r["created_at"],
            "sharpe": r["metrics"].get("sharpe"),
            "annual_return": r["metrics"].get("annual_return"),
            "max_drawdown": r["metrics"].get("max_drawdown"),
            "data_snapshot": r.get("data_snapshot"),
        }
        for r in runs
    ]
    return {"n_runs": len(compact), "runs": compact}


def _get_experiment(run_id: str) -> dict:
    run = _tracker().get_run(run_id)
    return {
        "run_id": run["id"],
        "name": run["name"],
        "kind": run["kind"],
        "created_at": run["created_at"],
        "config": run["config"],
        "metrics": run["metrics"],
        "artifacts_dir": run.get("artifacts_dir"),
        "data_snapshot": run.get("data_snapshot"),
        "notes": run.get("notes"),
    }


def _compare_experiments(run_ids: list[str]) -> dict:
    df = _tracker().compare(run_ids)
    return {
        "n_runs": len(run_ids),
        "table": json.loads(df.to_json(orient="index", force_ascii=False)),
        "note": "列名为 run_id；关注 sharpe/max_drawdown/annual_return 的横向差异",
    }


def _run_factor_analysis(
    factor_file: str,
    universe: list[str],
    start: str,
    end: str,
    params: dict[str, Any] | None = None,
    quantiles: int = 5,
    fwd_period: int = 1,
    name: str | None = None,
    notes: str | None = None,
) -> dict:
    from solidrock.factors import analyze_factor, load_factor_class

    universe_list = [s.value for s in validate_symbols(universe)]
    factor_cls = load_factor_class(factor_file)
    result = analyze_factor(
        factor_cls(**(params or {})),
        _store(),
        universe_list,
        start=start,
        end=end,
        quantiles=quantiles,
        fwd_period=fwd_period,
        log_experiment=True,
        name=name,
        notes=notes,
    )
    artifacts = (
        [str(result.artifacts_dir / f) for f in ("report.md", "result.json", "ic.csv", "layer_navs.csv")]
        if result.artifacts_dir is not None
        else []
    )
    return {
        "run_id": result.run_id,
        "factor": result.factor_name,
        "ic_summary": result.ic_summary,
        "factor_autocorr": result.factor_autocorr,
        "layer_stats": result.layer_stats.to_dict(orient="index"),
        "data_snapshot": result.data_snapshot,
        "artifacts": artifacts,
        "note": "解读见 SKILL：|IC|>0.03 且 ICIR>0.5 才值得继续；层间单调性比多空收益更重要",
    }


# ---------------------------------------------------------------------- 工具注册表
def tool_get_data_overview() -> str:
    """[工具] 本地数据总览：已缓存行情/日历/快照/实验数量."""
    return _run_tool(_data_overview)


def tool_data_health(symbols: list[str] | None = None, jump_threshold: float = 0.2) -> str:
    """[工具] 数据体检：缺失交易日/OHLC 异常/复权因子缺失/大幅波动。"""
    return _run_tool(_data_health, symbols=symbols, jump_threshold=jump_threshold)


def tool_search_instruments(query: str, limit: int = 20, source: str | None = None) -> str:
    """[工具] 按代码或名称搜索标的（股票/指数/ETF）。"""
    return _run_tool(_search_instruments, query=query, limit=limit, source=source)


def tool_fetch_bars(
    symbols: list[str] | str,
    start: str | None = None,
    end: str | None = None,
    source: str | None = None,
    with_adj_factor: bool = True,
    freq: str = "1d",
) -> str:
    """[工具] 拉取/更新行情到本地（增量幂等）。freq：1d / 1m / 5m。"""
    return _run_tool(
        _fetch_bars,
        symbols=symbols,
        start=start,
        end=end,
        source=source,
        with_adj_factor=with_adj_factor,
        freq=freq,
    )


def tool_get_trading_calendar(
    start: str | None = None,
    end: str | None = None,
    update: bool = False,
    source: str | None = None,
) -> str:
    """[工具] 查询（或更新）交易日历。"""
    return _run_tool(_trading_calendar, start=start, end=end, update=update, source=source)


def tool_validate_strategy(strategy_file: str) -> str:
    """[工具] 策略静态检查：语法/前视偏差/Context API 误用。"""
    return _run_tool(_validate_strategy, strategy_file=strategy_file)


def tool_run_backtest(
    strategy_file: str,
    start: str,
    end: str,
    params: dict[str, Any] | None = None,
    benchmark: str | None = "000300.SH",
    execution: str = "next_open",
    initial_cash: float = 1_000_000.0,
    name: str | None = None,
    notes: str | None = None,
) -> str:
    """[工具] 运行回测（自动先做静态校验，结果自动留痕）。"""
    return _run_tool(
        _run_backtest,
        strategy_file=strategy_file,
        start=start,
        end=end,
        params=params,
        benchmark=benchmark,
        execution=execution,
        initial_cash=initial_cash,
        name=name,
        notes=notes,
    )


def tool_list_experiments(kind: str | None = None, limit: int = 20) -> str:
    """[工具] 列出最近的实验（回测/因子分析）。"""
    return _run_tool(_list_experiments, kind=kind, limit=limit)


def tool_get_experiment(run_id: str) -> str:
    """[工具] 查看单个实验的完整配置与指标。"""
    return _run_tool(_get_experiment, run_id=run_id)


def tool_compare_experiments(run_ids: list[str]) -> str:
    """[工具] 并排对比多个实验的核心指标。"""
    return _run_tool(_compare_experiments, run_ids=run_ids)


def tool_run_factor_analysis(
    factor_file: str,
    universe: list[str],
    start: str,
    end: str,
    params: dict[str, Any] | None = None,
    quantiles: int = 5,
    fwd_period: int = 1,
    name: str | None = None,
    notes: str | None = None,
) -> str:
    """[工具] 运行因子分析（RankIC + 分层回测），自动留痕。"""
    return _run_tool(
        _run_factor_analysis,
        factor_file=factor_file,
        universe=universe,
        start=start,
        end=end,
        params=params,
        quantiles=quantiles,
        fwd_period=fwd_period,
        name=name,
        notes=notes,
    )


def tool_run_vectorized_backtest(
    factor_file: str,
    universe: list[str],
    start: str,
    end: str,
    params: dict[str, Any] | None = None,
    top: float = 0.2,
    bottom: float = 0.2,
    fee_rate: float = 1.5e-4,
    name: str | None = None,
) -> str:
    """[工具] 向量化因子筛选：多空分层组合的快速净值（秒级，用于迭代筛选）.

    与 run_factor_analysis 的区别：本工具直接给出"做多因子头部、做空尾部"
    的组合净值与夏普，适合批量扫参数；精确结论用 run_backtest 复核。
    """
    return _run_tool(
        _run_vectorized_backtest,
        factor_file=factor_file,
        universe=universe,
        start=start,
        end=end,
        params=params,
        top=top,
        bottom=bottom,
        fee_rate=fee_rate,
        name=name,
    )


def tool_run_backtest_sandboxed(
    strategy_file: str,
    start: str,
    end: str,
    params: dict[str, Any] | None = None,
    benchmark: str | None = "000300.SH",
    timeout: float = 300.0,
    name: str | None = None,
) -> str:
    """[工具] 沙箱回测：子进程隔离执行（死循环/崩溃的策略不会拖垮会话）.

    用法与 run_backtest 相同；超出 timeout 秒会被强制终止并返回 TIMEOUT
    错误。不确定策略质量时优先用本工具。
    """
    return _run_tool(
        _run_sandboxed_backtest,
        strategy_file=strategy_file,
        start=start,
        end=end,
        params=params,
        benchmark=benchmark,
        timeout=timeout,
        name=name,
    )


def tool_run_paper_session(
    strategy_file: str,
    name: str,
    start: str | None = None,
    end: str | None = None,
    params: dict[str, Any] | None = None,
    initial_cash: float = 1_000_000.0,
) -> str:
    """[工具] 运行模拟盘会话（状态持久化，建议每交易日收盘后调用一次）.

    首次调用初始化（start 为起点）；之后每次调用增量处理新交易日：
    昨日订单今日开盘撮合、今日收盘产出新订单。params 仅首次生效。
    """
    return _run_tool(
        _paper_session,
        strategy_file=strategy_file,
        name=name,
        start=start,
        end=end,
        params=params,
        initial_cash=initial_cash,
    )


def tool_paper_status(name: str) -> str:
    """[工具] 查看模拟盘当前状态（现金/持仓/待执行订单/最后处理日）。"""
    return _run_tool(_paper_status, name=name)


def _live_status() -> dict[str, Any]:
    from solidrock.live import CfquantBroker

    broker = CfquantBroker(read_only=True)
    return {"asset": broker.query_asset(), "positions": broker.query_positions()}


def _live_orders(cancelable_only: bool) -> dict[str, Any]:
    from solidrock.live import CfquantBroker

    return {"orders": CfquantBroker(read_only=True).query_orders(cancelable_only=cancelable_only)}


def _live_trades() -> dict[str, Any]:
    from solidrock.live import CfquantBroker

    return {"trades": CfquantBroker(read_only=True).query_trades()}


def _live_submit_order(
    symbol: str,
    side: str,
    qty: int,
    price: float | None,
    strategy_name: str,
    order_remark: str,
) -> dict[str, Any]:
    from solidrock.live import CfquantBroker

    broker = CfquantBroker(read_only=False)
    receipt = broker.submit_order(
        symbol,
        side,
        qty,
        price=price,
        strategy_name=strategy_name,
        order_remark=order_remark,
    )
    return {**receipt, "symbol": symbol, "side": side, "qty": qty, "price": price}


def _live_cancel_order(order_id: str) -> dict[str, Any]:
    from solidrock.live import CfquantBroker

    return CfquantBroker(read_only=False).cancel_order(order_id)


def _live_reconcile(paper_name: str | None, target: dict[str, int] | None) -> dict[str, Any]:
    from solidrock.live import CfquantBroker, diff_positions

    broker = CfquantBroker(read_only=True)
    actual = {
        p.get("stock_code", ""): int(p.get("can_use_volume", p.get("volume", 0)) or 0) for p in broker.query_positions()
    }
    if target is None and paper_name:
        from solidrock.backtest.paper import PaperTrader

        info = PaperTrader(
            "unknown",
            BacktestConfig(start="1990-01-01", end="2099-12-31", benchmark=None),
            _store(),
            name=paper_name,
        ).status()
        target = {s: int(d["shares"]) for s, d in info["positions"].items()}
    if target is None:
        raise err(ErrorCode.PARAM_INVALID, "需要 paper_name 或 target 二选一")
    actions = diff_positions(target, actual)
    return {"actual": actual, "target": target, "suggested_actions": actions}


def tool_live_status() -> str:
    """[工具] 查询 QMT 实盘账户资金与持仓（只读，需 cfquant 桥接在线）。"""
    return _run_tool(_live_status)


def tool_live_orders(cancelable_only: bool = False) -> str:
    """[工具] 查询实盘委托（可只查可撤委托）。"""
    return _run_tool(_live_orders, cancelable_only=cancelable_only)


def tool_live_trades() -> str:
    """[工具] 查询实盘成交。"""
    return _run_tool(_live_trades)


def tool_live_submit_order(
    symbol: str,
    side: str,
    qty: int,
    price: float | None = None,
    strategy_name: str = "solidrock-agent",
) -> str:
    """[工具] 向 QMT 提交实盘订单（真实资金！默认只读模式会拒绝）.

    side: buy/sell；qty: 股数（买入整手）；price: None=最新价市价。
    提交前请与用户二次确认标的、方向、数量。审计日志自动记录。
    """
    return _run_tool(
        _live_submit_order,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        strategy_name=strategy_name,
        order_remark="mcp",
    )


def tool_live_cancel_order(order_id: str) -> str:
    """[工具] 撤销实盘委托。"""
    return _run_tool(_live_cancel_order, order_id=order_id)


def tool_live_reconcile(paper_name: str | None = None, target: dict[str, int] | None = None) -> str:
    """[工具] 实盘对账：对比 QMT 实际持仓与目标（模拟盘持仓或手工指定）.

    只输出差异与建议动作，不自动下单。target 形如 {"510300.SH": 1000}。
    """
    return _run_tool(_live_reconcile, paper_name=paper_name, target=target)


def _run_ml_walk_forward(
    factor_names: list[str],
    universe: list[str],
    start: str,
    end: str,
    horizon: int = 5,
    train_window: int = 252,
    test_window: int = 21,
    step: int = 21,
) -> dict[str, Any]:
    """委托给 walk_forward_ml 管道（详见 solidrock.ml.pipeline）."""
    from solidrock.data.symbols import validate_symbols
    from solidrock.factors.base import create_factor
    from solidrock.ml.pipeline import walk_forward_ml

    universe_list = [s.value for s in validate_symbols(universe)]
    factors = {fname: create_factor(fname) for fname in factor_names}
    result = walk_forward_ml(
        factors,
        _store(),
        universe_list,
        start=start,
        end=end,
        horizon=horizon,
        train_window=train_window,
        test_window=test_window,
        step=step,
        log_experiment=True,
    )
    return {
        "run_id": result.run_id,
        "factor_names": result.factor_names,
        "ic_summary": result.ic_summary,
        "feature_importance": result.feature_importance,
        "window_stats": result.window_stats,
        "data_snapshot": result.data_snapshot,
        "artifacts_dir": str(result.artifacts_dir) if result.artifacts_dir else None,
        "note": "walk-forward ML 结果，过拟合检测见 window_stats 的 train_ic vs test_ic",
    }


def tool_run_ml_walk_forward(
    factor_names: list[str],
    universe: list[str],
    start: str,
    end: str,
    horizon: int = 5,
    train_window: int = 252,
    test_window: int = 21,
    step: int = 21,
) -> str:
    """[工具] Walk-forward ML 管道：滚动训练/预测 + 预测 IC + 向量化回测."""
    return _run_tool(
        _run_ml_walk_forward,
        factor_names=factor_names,
        universe=universe,
        start=start,
        end=end,
        horizon=horizon,
        train_window=train_window,
        test_window=test_window,
        step=step,
    )


def tool_list_data_sources() -> str:
    """[工具] 列出可用数据源及其能力。"""

    def _impl() -> dict:
        return {"sources": list_sources()}

    return _run_tool(_impl)


ALL_TOOLS: dict[str, Any] = {
    "get_data_overview": tool_get_data_overview,
    "data_health": tool_data_health,
    "list_data_sources": tool_list_data_sources,
    "search_instruments": tool_search_instruments,
    "fetch_bars": tool_fetch_bars,
    "get_trading_calendar": tool_get_trading_calendar,
    "validate_strategy": tool_validate_strategy,
    "run_backtest": tool_run_backtest,
    "list_experiments": tool_list_experiments,
    "get_experiment": tool_get_experiment,
    "compare_experiments": tool_compare_experiments,
    "run_factor_analysis": tool_run_factor_analysis,
    "run_vectorized_backtest": tool_run_vectorized_backtest,
    "run_ml_walk_forward": tool_run_ml_walk_forward,
    "run_backtest_sandboxed": tool_run_backtest_sandboxed,
    "run_paper_session": tool_run_paper_session,
    "paper_status": tool_paper_status,
    "live_status": tool_live_status,
    "live_orders": tool_live_orders,
    "live_trades": tool_live_trades,
    "live_submit_order": tool_live_submit_order,
    "live_cancel_order": tool_live_cancel_order,
    "live_reconcile": tool_live_reconcile,
}
