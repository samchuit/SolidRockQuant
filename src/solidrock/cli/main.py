"""srq —— SolidRockQuant 命令行.

命令总览（v0.1 M2 实现数据子命令，回测/实验/MCP 随 M3-M6 加入）::

    srq init                          初始化数据目录
    srq config show                   查看配置（token 脱敏）
    srq data update    --symbols ...  拉取/更新日线到本地
    srq data calendar  --update       拉取/查看交易日历
    srq data peek      SYMBOL         查看已缓存数据的首尾样本
    srq data stats                    已缓存符号统计
    srq data snapshot create|list|delete   数据版本快照
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from solidrock.agent.errors import ErrorCode, SolidRockError
from solidrock.config import get_settings
from solidrock.data.calendar import TradingCalendar
from solidrock.data.sources import create_source, list_sources
from solidrock.data.store import DataStore
from solidrock.data.symbols import validate_symbols

app = typer.Typer(
    name="srq",
    help="SolidRockQuant · 磐石智擎 — Agent 原生的量化研究与回测框架",
    no_args_is_help=True,
    add_completion=False,
)
data_app = typer.Typer(help="数据管理：更新、日历、快照", no_args_is_help=True)
app.add_typer(data_app, name="data")

console = Console()
err_console = Console(stderr=True, style="red")


def _store() -> DataStore:
    return DataStore(get_settings().resolved_data_dir())


def _print_error(e: SolidRockError) -> None:
    err_console.print(f"[{e.code.value}] {e.message}")
    if e.hint:
        err_console.print(f"hint: {e.hint}", style="yellow")


# ----------------------------------------------------------------- 基础命令
@app.command()
def init() -> None:
    """初始化数据目录并显示配置摘要。"""
    settings = get_settings()
    data_dir = settings.resolved_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[green]✓[/green] 数据目录：{data_dir}")
    token_state = "已配置" if settings.tushare_token else "未配置（Tushare 数据源不可用）"
    console.print(f"Tushare token：{token_state}")
    if not settings.tushare_token:
        console.print("  配置方法：环境变量 SOLIDROCK_TUSHARE_TOKEN 或项目根 .env 文件", style="dim")
    console.print("下一步：srq data update --symbols 000001.SZ --start 2024-01-01")


@app.command("config")
def config_show() -> None:
    """显示当前配置（敏感信息脱敏）。"""
    settings = get_settings()
    table = Table(title="SolidRockQuant 配置", show_header=False)
    table.add_column("键", style="cyan")
    table.add_column("值")
    table.add_row("data_dir", str(settings.resolved_data_dir()))
    table.add_row("default_source", settings.default_source)
    token = settings.tushare_token
    masked = f"{token[:4]}****{token[-4:]}" if token and len(token) >= 8 else ("（未配置）" if not token else "****")
    table.add_row("tushare_token", masked)
    table.add_row("commission_rate", f"{settings.commission_rate:.6f}（万{settings.commission_rate * 1e4:.1f}）")
    table.add_row("stamp_duty_rate", f"{settings.stamp_duty_rate:.6f}")
    console.print(table)


# ----------------------------------------------------------------- data 子命令
@data_app.command("update")
def data_update(
    symbols: str = typer.Option(..., "--symbols", "-s", help="逗号分隔的统一符号，如 000001.SZ,600519.SH"),
    source: str | None = typer.Option(None, "--source", help="数据源名（默认取配置）"),
    start: str | None = typer.Option(None, "--start", help="起始日期 YYYY-MM-DD（缺省增量续拉）"),
    end: str | None = typer.Option(None, "--end", help="结束日期 YYYY-MM-DD（缺省到今天）"),
    full: bool = typer.Option(False, "--full", help="忽略增量，从 --start 全量重拉"),
    freq: str = typer.Option("1d", "--freq", help="频率：1d / 1m / 5m"),
    no_adj: bool = typer.Option(False, "--no-adj", help="跳过复权因子（更快，但前复权不可用）"),
) -> None:
    """拉取/更新行情到本地仓库（增量幂等）。"""
    settings = get_settings()
    symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    try:
        validate_symbols(symbol_list)
        src = create_source(source or settings.default_source)
        store = _store()
        fetch_start = start
        if fetch_start is None and not full:
            fetch_start = _incremental_start(store, symbol_list, freq=freq)
        with console.status(f"[cyan]{src.name} 拉取 {len(symbol_list)} 个标的（{freq}）…"):
            df = src.fetch_bars(symbol_list, start=fetch_start, end=end, freq=freq, with_adj_factor=not no_adj)
            if df.empty:
                raise SolidRockError(
                    ErrorCode.NO_DATA,
                    f"数据源 {src.name} 未返回任何数据"
                    + (f"（区间 {fetch_start} ~ {end}）" if fetch_start or end else ""),
                    hint="检查符号与日期区间；首次拉取建议显式给 --start",
                )
            totals = store.update_bars(df, freq=freq, source=src.name)
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e

    table = Table(title=f"已更新（来源 {src.name}）")
    table.add_column("符号", style="cyan")
    table.add_column("本地总行数", justify="right")
    for symbol, rows in sorted(totals.items()):
        table.add_row(symbol, str(rows))
    console.print(table)
    console.print(
        "下一步：srq data peek <符号> 查看样本；srq data snapshot create <tag> 固定数据版本",
        style="dim",
    )


def _incremental_start(store: DataStore, symbol_list: list[str], *, freq: str = "1d") -> str:
    """增量起点：全部符号都有本地数据时，从最早的最后日期续拉；否则全量.

    日线：取最后日期次日（当日数据不会新增）；
    分钟线：取最后日期当日零点起重拉（同日晚些时候还会有新 bar，
    update_bars 按 (symbol, date) 去重保证幂等）。
    """
    last_dates = [store.last_date(s, freq=freq) for s in symbol_list]
    if all(d is not None for d in last_dates):
        earliest = min(d for d in last_dates if d is not None)
        if freq == "1d":
            return (earliest + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        return earliest.strftime("%Y-%m-%d")
    return "1990-01-01"


@data_app.command("calendar")
def data_calendar(
    update: bool = typer.Option(False, "--update", help="从数据源拉取并缓存"),
    source: str | None = typer.Option(None, "--source"),
) -> None:
    """查看/更新交易日历。"""
    try:
        store = _store()
        if update:
            settings = get_settings()
            src = create_source(source or settings.default_source)
            n = TradingCalendar(store).update(src)
            console.print(f"[green]✓[/green] 交易日历已更新（{src.name}），共 {n} 个交易日")
        cal = store.load_calendar()
        if cal is None or cal.empty:
            console.print("本地暂无交易日历；运行 [cyan]srq data calendar --update[/cyan] 拉取", style="yellow")
            return
        console.print(f"覆盖区间：{cal['date'].min().date()} ~ {cal['date'].max().date()}，共 {len(cal)} 个交易日")
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e


@data_app.command("peek")
def data_peek(
    symbol: str = typer.Argument(..., help="统一符号，如 000001.SZ"),
    rows: int = typer.Option(3, "--rows", "-n", help="首尾各显示行数"),
    freq: str = typer.Option("1d", "--freq", help="频率：1d / 1m / 5m"),
) -> None:
    """查看已缓存数据的首尾样本与覆盖区间。"""
    try:
        store = _store()
        df = store.load_bars(symbol, freq=freq)
        if df.empty:
            raise SolidRockError(
                ErrorCode.NO_DATA,
                f"本地没有 {symbol} 的数据",
                hint="先执行 srq data update --symbols " + symbol,
            )
        console.print(f"{symbol}：{len(df)} 行，{df['date'].min().date()} ~ {df['date'].max().date()}")
        with pd.option_context("display.width", 160, "display.max_columns", None):
            console.print(df.head(rows).to_string(index=False))
        console.print("…")
        console.print(df.tail(rows).to_string(index=False))
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e


@data_app.command("stats")
def data_stats() -> None:
    """已缓存数据统计（按数据源）。"""
    try:
        store = _store()
        prov = store.provenance()
        symbols = store.symbols()
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e
    if not symbols:
        console.print("本地暂无缓存数据；运行 [cyan]srq data update --symbols ...[/cyan] 开始", style="yellow")
        return
    table = Table(title=f"本地数据（{len(symbols)} 个符号）")
    table.add_column("符号", style="cyan")
    table.add_column("行数", justify="right")
    table.add_column("来源")
    table.add_column("最后更新")
    for symbol in symbols:
        meta = prov.get(symbol, {})
        table.add_row(
            symbol,
            str(meta.get("rows", "-")),
            str(meta.get("source") or "-"),
            str(meta.get("updated_at", "-"))[:19],
        )
    console.print(table)


@data_app.command("sources")
def data_sources() -> None:
    """列出可用数据源及其能力。"""
    rows = list_sources()
    table = Table(title="数据源")
    table.add_column("名称", style="cyan")
    table.add_column("可用", justify="center")
    table.add_column("能力")
    for row in rows:
        table.add_row(
            row["name"],
            "[green]✓[/green]" if row["available"] else "[red]✗[/red]（pip install 'solidrock-quant[sources]'）",
            "\n".join(row["capabilities"]),
        )
    console.print(table)


@data_app.command("doctor")
def data_doctor(
    symbols: str | None = typer.Option(None, "--symbols", "-s", help="逗号分隔（缺省检查全部）"),
    jump: float = typer.Option(0.2, "--jump-threshold", help="单日涨跌幅告警阈值"),
) -> None:
    """数据体检：缺失/逻辑错误/复权因子缺失/异常波动。"""
    from solidrock.data.quality import check_store, render_health_markdown

    try:
        symbol_list = [s.strip().upper() for s in symbols.split(",")] if symbols else None
        report = check_store(_store(), symbol_list, jump_threshold=jump)
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e
    if report.ok:
        console.print(f"[green]✓[/green] 体检通过：{report.checked} 个符号全部健康")
        return
    from rich.markdown import Markdown

    console.print(Markdown(render_health_markdown(report)))


snapshot_app = typer.Typer(help="数据版本快照（实验复现用）", no_args_is_help=True)
data_app.add_typer(snapshot_app, name="snapshot")


@snapshot_app.command("create")
def snapshot_create(
    tag: str = typer.Argument(..., help="快照名，如 snap-20260908"),
) -> None:
    """创建不可变快照（硬链接，近零拷贝）。"""
    try:
        store = _store()
        snap = store.create_snapshot(tag)
        console.print(f"[green]✓[/green] 快照 {snap.snapshot} 已创建（只读）")
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e


@snapshot_app.command("list")
def snapshot_list() -> None:
    """列出全部快照。"""
    snaps = _store().list_snapshots()
    if not snaps:
        console.print("（无快照）", style="dim")
        return
    for s in snaps:
        console.print(f"  {s}")


@snapshot_app.command("delete")
def snapshot_delete(
    tag: str = typer.Argument(...),
    force: bool = typer.Option(False, "--force", "-f", help="跳过确认"),
) -> None:
    """删除快照。"""
    if not force and not typer.confirm(f"确认删除快照 {tag}？"):
        raise typer.Abort()
    try:
        _store().delete_snapshot(tag)
        console.print(f"[green]✓[/green] 快照 {tag} 已删除")
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e


# ----------------------------------------------------------------- backtest
backtest_app = typer.Typer(help="回测", no_args_is_help=True)
app.add_typer(backtest_app, name="backtest")

experiment_app = typer.Typer(help="实验追踪查询", no_args_is_help=True)
app.add_typer(experiment_app, name="experiment")


@backtest_app.command("run")
def backtest_run(
    strategy_file: Path = typer.Argument(..., help="策略文件路径（内含一个 Strategy 子类）"),
    start: str = typer.Option(..., "--start", help="回测起始日 YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="回测结束日 YYYY-MM-DD"),
    benchmark: str | None = typer.Option("000300.SH", "--benchmark", help="基准（none 关闭）"),
    cash: float = typer.Option(1_000_000.0, "--cash", help="初始资金"),
    execution: str = typer.Option("next_open", "--execution", help="next_open | same_close"),
    param: list[str] = typer.Option([], "--param", "-p", help="策略参数 k=v，可多次"),
    name: str | None = typer.Option(None, "--name", help="实验名"),
    no_experiment: bool = typer.Option(False, "--no-experiment", help="不写入实验追踪"),
) -> None:
    """运行回测：产出 report.md / result.json / trades.csv / nav.csv 并自动留痕."""
    from solidrock.backtest import BacktestConfig, BacktestEngine
    from solidrock.report.metrics import format_metrics
    from solidrock.strategy.loader import load_strategy_class, parse_param_pairs

    params = parse_param_pairs(list(param))
    strategy_cls = load_strategy_class(strategy_file)
    cfg = BacktestConfig(
        start=start,
        end=end,
        benchmark=None if benchmark and benchmark.lower() == "none" else benchmark,
        execution=execution,  # type: ignore[arg-type]
        initial_cash=cash,
        name=name,
        log_experiment=not no_experiment,
    )
    try:
        result = BacktestEngine(strategy_cls(**params), cfg, _store()).run()
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e

    table = Table(title=f"回测完成 · {result.run_id}")
    table.add_column("指标", style="cyan")
    table.add_column("数值", justify="right")
    for label, value in format_metrics(result.metrics):
        table.add_row(label, value)
    console.print(table)
    if not result.rejections.empty:
        codes = result.rejections["code"].value_counts().to_dict()
        console.print(f"拒单：{codes}", style="yellow")
    console.print(f"产物目录：{result.artifacts_dir}")
    console.print("对比历史：srq experiment list / srq experiment compare <id1> <id2>", style="dim")


# ----------------------------------------------------------------- factor
factor_app = typer.Typer(help="因子研究", no_args_is_help=True)
app.add_typer(factor_app, name="factor")


@factor_app.command("analyze")
def factor_analyze(
    factor_file: Path = typer.Argument(..., help="因子文件路径（内含一个 Factor 子类）"),
    universe: str = typer.Option(..., "--universe", "-u", help="逗号分隔的股票池符号"),
    start: str = typer.Option(..., "--start"),
    end: str = typer.Option(..., "--end"),
    quantiles: int = typer.Option(5, "--quantiles", "-q", help="分层数"),
    fwd_period: int = typer.Option(1, "--fwd-period", help="前瞻收益期（交易日）"),
    param: list[str] = typer.Option([], "--param", "-p", help="因子参数 k=v，可多次"),
    name: str | None = typer.Option(None, "--name"),
    no_experiment: bool = typer.Option(False, "--no-experiment"),
) -> None:
    """运行因子分析：RankIC + 分层回测，自动留痕."""
    from solidrock.factors import analyze_factor, load_factor_class
    from solidrock.strategy.loader import parse_param_pairs

    universe_list = [s.strip().upper() for s in universe.split(",") if s.strip()]
    try:
        validate_symbols(universe_list)
        factor_cls = load_factor_class(factor_file)
        result = analyze_factor(
            factor_cls(**parse_param_pairs(list(param))),
            _store(),
            universe_list,
            start=start,
            end=end,
            quantiles=quantiles,
            fwd_period=fwd_period,
            log_experiment=not no_experiment,
            name=name,
        )
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e

    ic = result.ic_summary
    table = Table(title=f"因子分析完成 · {result.run_id}")
    table.add_column("指标", style="cyan")
    table.add_column("数值", justify="right")
    table.add_row("IC 均值", f"{ic['ic_mean']:.4f}")
    table.add_row("ICIR", f"{ic['ic_ir']:.3f}")
    table.add_row("IC t 值", f"{ic['ic_t_stat']:.2f}")
    table.add_row("IC>0 占比", f"{ic['positive_ratio'] * 100:.1f}%")
    table.add_row("因子自相关", f"{result.factor_autocorr:.3f}")
    for layer, row in result.layer_stats.iterrows():
        label = "多空L-S" if str(layer) == "L-S" else f"层{layer}"
        table.add_row(f"{label} 年化", f"{row['annual_return'] * 100:.2f}%")
    console.print(table)
    console.print(f"产物目录：{result.artifacts_dir}", style="dim")


@factor_app.command("screen")
def factor_screen(
    factor_file: Path = typer.Argument(..., help="因子文件路径（内含一个 Factor 子类）"),
    universe: str = typer.Option(..., "--universe", "-u", help="逗号分隔的股票池符号"),
    start: str = typer.Option(..., "--start"),
    end: str = typer.Option(..., "--end"),
    top: float = typer.Option(0.2, "--top", help="做多头部比例"),
    bottom: float = typer.Option(0.2, "--bottom", help="做空尾部比例"),
    fee_rate: float = typer.Option(1.5e-4, "--fee-rate", help="单边费率"),
    param: list[str] = typer.Option([], "--param", "-p", help="因子参数 k=v，可多次"),
    name: str | None = typer.Option(None, "--name"),
) -> None:
    """向量化因子筛选：多空组合快速净值（秒级，用于扫参数）。"""
    import json as _json

    from solidrock.agent.tools import tool_run_vectorized_backtest
    from solidrock.strategy.loader import parse_param_pairs

    universe_list = [s.strip().upper() for s in universe.split(",") if s.strip()]
    try:
        validate_symbols(universe_list)
        raw = tool_run_vectorized_backtest(
            factor_file=str(factor_file),
            universe=universe_list,
            start=start,
            end=end,
            params=parse_param_pairs(list(param)),
            top=top,
            bottom=bottom,
            fee_rate=fee_rate,
            name=name,
        )
        envelope = _json.loads(raw)
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e
    if envelope["status"] != "ok":
        from solidrock.agent.errors import ErrorCode as _ec
        from solidrock.agent.errors import err as _err

        _print_error(_err(_ec.INTERNAL_ERROR, "向量化回测失败", details=envelope.get("error")))
        raise typer.Exit(1)
    data = envelope["data"]
    m = data["metrics"]
    table = Table(title=f"向量化筛选完成 · {data['run_id']}")
    table.add_column("指标", style="cyan")
    table.add_column("数值", justify="right")
    table.add_row("累计收益", f"{m.get('total_return', 0) * 100:.2f}%")
    table.add_row("年化收益", f"{m.get('annual_return', 0) * 100:.2f}%")
    table.add_row("夏普", f"{m.get('sharpe', 0):.2f}")
    table.add_row("最大回撤", f"{m.get('max_drawdown', 0) * 100:.2f}%")
    table.add_row("年化换手", f"{m.get('annual_turnover', 0) * 100:.0f}%")
    console.print(table)
    console.print(f"产物目录：{data['artifacts'][0]}", style="dim")


# ----------------------------------------------------------------- paper
paper_app = typer.Typer(help="模拟盘（状态持久化的日频 paper trading）", no_args_is_help=True)
app.add_typer(paper_app, name="paper")


@paper_app.command("run")
def paper_run(
    strategy_file: Path = typer.Argument(..., help="策略文件路径（内含一个 Strategy 子类）"),
    name: str = typer.Option(..., "--name", help="模拟盘名称（状态持久化的键）"),
    start: str | None = typer.Option(None, "--start", help="首次运行的起点 YYYY-MM-DD"),
    end: str | None = typer.Option(None, "--end", help="处理到哪一天（默认今天）"),
    cash: float = typer.Option(1_000_000.0, "--cash", help="初始资金（仅首次运行生效）"),
    param: list[str] = typer.Option([], "--param", "-p", help="策略参数 k=v（仅首次运行生效）"),
) -> None:
    """运行模拟盘：首次初始化并跑到最新交易日；之后增量处理新交易日.

    建议每交易日收盘后运行一次（可由系统计划任务调度）。
    """
    from solidrock.backtest import BacktestConfig
    from solidrock.backtest.paper import PaperTrader
    from solidrock.strategy.loader import load_strategy_class, parse_param_pairs

    params = parse_param_pairs(list(param))
    cfg = BacktestConfig(
        start=start or "1990-01-01", end=end or "2099-12-31", benchmark=None, initial_cash=cash, log_experiment=False
    )
    try:
        load_strategy_class(strategy_file)  # 提前校验文件可加载
        trader = PaperTrader(str(strategy_file), cfg, _store(), name=name, params=params)
        summary = trader.run(end=end)
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e

    if not summary.get("ran"):
        console.print(f"模拟盘 [cyan]{name}[/cyan]：{summary['note']}")
        return
    table = Table(title=f"模拟盘 {name} · 处理至 {summary['last_date']}")
    table.add_column("项目", style="cyan")
    table.add_column("数值", justify="right")
    table.add_row("现金", f"{summary['cash']:,.2f}")
    for symbol, pos in summary["positions"].items():
        table.add_row(f"持仓 {symbol}", f"{pos['shares']:.0f} 股 @ {pos['last_price']:.2f}")
    table.add_row("今日成交", f"{len(summary['trades'])} 笔")
    table.add_row("待执行订单", f"{len(summary['pending_orders'])} 笔")
    console.print(table)


@paper_app.command("status")
def paper_status(
    name: str = typer.Option(..., "--name"),
) -> None:
    """查看模拟盘当前状态。"""
    from solidrock.backtest import BacktestConfig
    from solidrock.backtest.paper import PaperTrader

    try:
        info = PaperTrader(
            "unknown", BacktestConfig(start="1990-01-01", end="2099-12-31"), _store(), name=name
        ).status()
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e
    table = Table(title=f"模拟盘 {name}")
    table.add_column("项目", style="cyan")
    table.add_column("数值")
    table.add_row("策略", info["strategy"])
    table.add_row("最后处理日", str(info["last_date"]))
    table.add_row("现金", f"{info['cash']:,.2f}")
    for symbol, pos in info["positions"].items():
        table.add_row(f"持仓 {symbol}", f"{pos['shares']:.0f} 股 @ {pos['last_price']:.2f}")
    table.add_row("待执行订单", str(len(info["pending_orders"])))
    table.add_row("运行次数", str(info["run_count"]))
    console.print(table)


# ----------------------------------------------------------------- live
live_app = typer.Typer(help="实盘（QMT / cfquant 桥接）——默认只读，下单需显式解除只读", no_args_is_help=True)
app.add_typer(live_app, name="live")


def _broker(read_only: bool | None = None) -> Any:
    from solidrock.live import CfquantBroker

    return CfquantBroker(read_only=read_only, data_dir=str(get_settings().resolved_data_dir()))


@live_app.command("status")
def live_status() -> None:
    """查询 QMT 账户资金与持仓（只读）。"""
    try:
        broker = _broker(read_only=True)
        asset = broker.query_asset()
        positions = broker.query_positions()
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e
    console.print_json(json.dumps({"asset": asset}, ensure_ascii=False, default=str))
    if positions:
        table = Table(title="持仓")
        for col in ("标的", "数量", "可用", "成本", "现价", "市值"):
            table.add_column(col, justify="right")
        for p in positions:
            table.add_row(
                str(p.get("stock_code", "-")),
                str(p.get("volume", "-")),
                str(p.get("can_use_volume", p.get("open_volume", "-"))),
                str(p.get("avg_price", p.get("open_price", "-"))),
                str(p.get("market_value", "-")),
                str(p.get("market_value", "-")),
            )
        console.print(table)
    else:
        console.print("（无持仓）", style="dim")


@live_app.command("orders")
def live_orders(
    cancelable_only: bool = typer.Option(False, "--cancelable", help="只查可撤委托"),
) -> None:
    """查询委托。"""
    try:
        orders = _broker(read_only=True).query_orders(cancelable_only=cancelable_only)
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e
    if not orders:
        console.print("（无委托）", style="dim")
        return
    console.print_json(json.dumps(orders, ensure_ascii=False, default=str))


@live_app.command("trades")
def live_trades() -> None:
    """查询成交。"""
    try:
        trades = _broker(read_only=True).query_trades()
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e
    if not trades:
        console.print("（无成交）", style="dim")
        return
    console.print_json(json.dumps(trades, ensure_ascii=False, default=str))


@live_app.command("order")
def live_order(
    symbol: str = typer.Option(..., "--symbol", "-s", help="统一符号，如 000001.SZ"),
    side: str = typer.Option(..., "--side", help="buy / sell"),
    qty: int = typer.Option(..., "--qty", help="股数（买入须整手）"),
    price: float | None = typer.Option(None, "--price", help="限价（缺省为最新价市价单）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印订单参数，不提交"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过交互确认"),
    read_only: bool | None = typer.Option(None, "--read-only/--no-read-only", help="覆盖只读配置"),
) -> None:
    """提交实盘订单（默认只读模式会拒绝；真实资金，请谨慎）。"""
    from solidrock.backtest.costs import AShareCostModel  # noqa: F401
    from solidrock.live import CfquantBroker

    sym_s: str = symbol.upper()
    side_s: str = side.lower()
    params = {
        "symbol": sym_s,
        "side": side_s,
        "qty": qty,
        "price": price,
        "price_type": "FIX_PRICE" if price is not None else "LATEST_PRICE",
    }
    console.print_json(json.dumps(params, ensure_ascii=False))
    if dry_run:
        console.print("[yellow]dry-run：未提交[/yellow]")
        return
    if not yes and not typer.confirm(
        f"确认向实盘提交 {side.upper()} {symbol} {qty} 股（{'限价 ' + str(price) if price else '最新价'}）？"
    ):
        raise typer.Abort()
    try:
        broker = CfquantBroker(read_only=False, data_dir=str(get_settings().resolved_data_dir()))
        receipt = broker.submit_order(
            symbol=sym_s,
            side=side_s,
            qty=qty,
            price=price,
        )
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e
    console.print(f"[green]✓[/green] 已提交：order_id={receipt['order_id']}")
    console.print("审计日志：.solidrock/live/audit.jsonl", style="dim")


@live_app.command("cancel")
def live_cancel(
    order_id: str = typer.Option(..., "--order-id"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    read_only: bool | None = typer.Option(None, "--read-only/--no-read-only"),
) -> None:
    """撤销委托。"""
    if not yes and not typer.confirm(f"确认撤销委托 {order_id}？"):
        raise typer.Abort()
    try:
        result = _broker(read_only=False).cancel_order(order_id)
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e
    console.print(f"[green]✓[/green] 撤单结果：{result}")


@live_app.command("reconcile")
def live_reconcile(
    paper_name: str | None = typer.Option(None, "--paper", help="对比指定模拟盘的目标持仓"),
    target: str | None = typer.Option(None, "--target", help="逗号分隔的目标持仓，如 510300.SH:1000,000001.SZ:200"),
    yes: bool = typer.Option(False, "--yes", "-y", help="自动按差异下单调仓（真实资金！）"),
) -> None:
    """实盘对账：对比 QMT 实际持仓与目标（模拟盘或手工指定），输出差异。"""
    from solidrock.live import diff_positions, render_reconcile_markdown

    try:
        broker = _broker(read_only=True)
        actual = {
            p.get("stock_code", ""): int(p.get("can_use_volume", p.get("volume", 0)) or 0)
            for p in broker.query_positions()
        }
        tgt: dict[str, int] = {}
        if target:
            for part in target.split(","):
                if ":" in part:
                    sym, qty = part.rsplit(":", 1)
                    tgt[sym.strip().upper()] = int(qty)
        elif paper_name:
            from solidrock.backtest import BacktestConfig
            from solidrock.backtest.paper import PaperTrader

            info = PaperTrader(
                "unknown",
                BacktestConfig(start="1990-01-01", end="2099-12-31", benchmark=None),
                _store(),
                name=paper_name,
            ).status()
            tgt = {s: int(d["shares"]) for s, d in info["positions"].items()}
        else:
            console.print("请指定 --paper <模拟盘名> 或 --target 510300.SH:1000,...", style="yellow")
            raise typer.Exit(1)
        actions = diff_positions(tgt, actual)
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e

    md = render_reconcile_markdown(actual, tgt, actions)
    from rich.markdown import Markdown

    console.print(Markdown(md))

    if actions and yes:
        if not typer.confirm("以上差异将按建议动作向实盘提交真实订单，确认？"):
            raise typer.Abort()
        try:
            broker = _broker(read_only=False)
            for a in actions:
                receipt = broker.submit_order(a["symbol"], a["action"], a["qty"], price=None)
                console.print(f"✓ {a['action'].upper()} {a['symbol']} {a['qty']} 股 → order_id={receipt['order_id']}")
        except SolidRockError as e:
            _print_error(e)
            raise typer.Exit(1) from e


# ----------------------------------------------------------------- experiment
@experiment_app.command("list")
def experiment_list(
    kind: str | None = typer.Option(None, "--kind", help="按类型过滤（backtest/factor）"),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """列出最近的实验。"""
    from solidrock.experiments.tracker import ExperimentTracker

    tracker = ExperimentTracker(get_settings().resolved_data_dir() / "experiments.db")
    runs = tracker.list_runs(kind=kind, limit=limit)
    if not runs:
        console.print("（暂无实验记录；运行 srq backtest run 后自动留痕）", style="dim")
        return
    table = Table(title=f"实验（最近 {len(runs)} 条）")
    table.add_column("run_id", style="cyan")
    table.add_column("名称")
    table.add_column("年化", justify="right")
    table.add_column("最大回撤", justify="right")
    table.add_column("夏普", justify="right")
    table.add_column("数据快照")
    for run in runs:
        m = run["metrics"]
        table.add_row(
            run["id"],
            run["name"],
            f"{m.get('annual_return', 0) * 100:.1f}%",
            f"{m.get('max_drawdown', 0) * 100:.1f}%",
            f"{m.get('sharpe', 0):.2f}",
            run.get("data_snapshot") or "-",
        )
    console.print(table)


@experiment_app.command("show")
def experiment_show(run_id: str = typer.Argument(...)) -> None:
    """查看单个实验的完整配置与指标。"""
    import json

    from solidrock.experiments.tracker import ExperimentTracker

    tracker = ExperimentTracker(get_settings().resolved_data_dir() / "experiments.db")
    try:
        run = tracker.get_run(run_id)
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e
    console.print(f"[cyan]{run['id']}[/cyan] · {run['name']} · {run['kind']} · {run['created_at']}")
    console.print("配置：", json.dumps(run["config"], ensure_ascii=False, indent=2))
    console.print("指标：", json.dumps(run["metrics"], ensure_ascii=False, indent=2))
    if run.get("artifacts_dir"):
        console.print(f"产物：{run['artifacts_dir']}")


@experiment_app.command("compare")
def experiment_compare(run_ids: list[str] = typer.Argument(..., help="至少两个 run_id")) -> None:
    """并排对比多个实验的核心指标。"""
    from solidrock.experiments.tracker import ExperimentTracker

    tracker = ExperimentTracker(get_settings().resolved_data_dir() / "experiments.db")
    try:
        df = tracker.compare(list(run_ids))
    except SolidRockError as e:
        _print_error(e)
        raise typer.Exit(1) from e
    console.print(df.to_string())


# ----------------------------------------------------------------- mcp
mcp_app = typer.Typer(help="MCP Server（Agent 接入）", no_args_is_help=True)
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("serve")
def mcp_serve() -> None:
    """启动 MCP Server（stdio 传输，供 Claude Desktop / Claude Code 接入）。"""
    from solidrock.agent.mcp_server import main as mcp_main

    mcp_main()


def main() -> None:
    """入口：统一把 SolidRockError 渲染为友好的错误输出。"""
    try:
        app()
    except SolidRockError as e:
        _print_error(e)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
