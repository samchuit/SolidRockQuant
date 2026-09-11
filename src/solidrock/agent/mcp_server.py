"""MCP Server（FastMCP，stdio 传输）.

接入 Claude Desktop / Claude Code 等 LLM 客户端::

    # .mcp.json
    {"mcpServers": {"solidrock": {"command": "srq", "args": ["mcp", "serve"]}}}

所有工具返回 JSON 字符串（统一信封，见 tools.py）。工具的 docstring 即
LLM 看到的使用说明，必须自包含、带单位与示例。
"""

from __future__ import annotations

from typing import Any

from solidrock import __version__
from solidrock.agent.errors import ErrorCode, err


def build_server() -> Any:
    """构建 MCP Server 实例（兼容 mcp 1.x FastMCP 与 2.x MCPServer）."""
    try:  # mcp 2.x
        from mcp.server.mcpserver import MCPServer as _Server
    except ImportError:
        try:  # mcp 1.x（fastmcp 已在 2.x 改名，此处兼容旧版）
            from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[attr-defined, no-redef]
        except ImportError as exc:
            raise err(
                ErrorCode.SOURCE_UNAVAILABLE,
                "MCP 扩展未安装",
                hint="pip install 'solidrock-quant[mcp]' 或 pip install mcp",
            ) from exc

    from solidrock.agent import tools as t

    mcp = _Server(
        "solidrock",
        instructions=(
            "SolidRockQuant：A股/期货量化研究与回测框架。"
            "典型研究闭环：get_data_overview → fetch_bars(补数据) → 编写策略文件 → "
            "validate_strategy → run_backtest → compare_experiments 迭代改进。"
            f"当前版本 {__version__}。"
        ),
    )

    @mcp.tool()
    def get_data_overview() -> str:
        """查看本地数据总览：已缓存的行情（符号数/行数/日期范围）、交易日历、快照、实验数量。

        开始任何研究前先调用它了解本地有什么数据。返回 JSON 信封。
        """
        return t.tool_get_data_overview()

    @mcp.tool()
    def data_health(symbols: list[str] | None = None, jump_threshold: float = 0.2) -> str:
        """数据体检：缺失交易日、OHLC 异常、复权因子缺失、单日大幅波动。

        symbols 缺省检查全部本地标的。回测前建议跑一次，避免坏数据污染结论。
        """
        return t.tool_data_health(symbols=symbols, jump_threshold=jump_threshold)

    @mcp.tool()
    def list_data_sources() -> str:
        """列出可用数据源（akshare/tushare 等）及其能力与可用性。"""
        return t.tool_list_data_sources()

    @mcp.tool()
    def search_instruments(query: str, limit: int = 20) -> str:
        """按代码或名称搜索标的。

        示例：query="510300" 找沪深300ETF；query="贵州茅台" 找 600519.SH。
        股票符号格式：<6位代码>.<SZ|SH|BJ>，指数/ETF 同理，期货为 <品种><月份>.<交易所>。
        """
        return t.tool_search_instruments(query=query, limit=limit)

    @mcp.tool()
    def fetch_bars(
        symbols: list[str] | str,
        start: str | None = None,
        end: str | None = None,
        source: str | None = None,
        freq: str = "1d",
    ) -> str:
        """拉取/更新行情到本地（增量幂等）。

        symbols 为统一符号或其列表，如 ["000001.SZ", "510300.SH"]；
        start/end 格式 YYYY-MM-DD（建议显式给 start）；freq：1d（默认）/
        1m / 5m（分钟线无复权因子，仅供研究，事件回测用日线）。
        """
        return t.tool_fetch_bars(symbols=symbols, start=start, end=end, source=source, freq=freq)

    @mcp.tool()
    def get_trading_calendar(
        start: str | None = None,
        end: str | None = None,
        update: bool = False,
    ) -> str:
        """查询交易日历（update=True 时先从数据源刷新）。"""
        return t.tool_get_trading_calendar(start=start, end=end, update=update)

    @mcp.tool()
    def validate_strategy(strategy_file: str) -> str:
        """策略代码静态检查：语法、前视偏差（shift(-n)/bfill）、Context API 误用。

        回测前必调用（run_backtest 内部也会自动调用）。strategy_file 为策略
        .py 文件的绝对路径。返回 issues 列表（severity: error/warning）。
        """
        return t.tool_validate_strategy(strategy_file=strategy_file)

    @mcp.tool()
    def run_backtest(
        strategy_file: str,
        start: str,
        end: str,
        params: dict[str, Any] | None = None,
        benchmark: str | None = "000300.SH",
        execution: str = "next_open",
        initial_cash: float = 1_000_000.0,
        name: str | None = None,
    ) -> str:
        """运行回测（自动静态校验 + 结果自动留痕）。

        strategy_file：策略文件绝对路径（内含一个 Strategy 子类）；
        params：策略参数覆盖，如 {"fast": 10}；
        execution：next_open（默认，防前视）| same_close（仅快速研究）；
        benchmark 传 null 关闭基准对比。返回核心指标 + 产物目录
        （report.md/result.json/trades.csv/nav.csv）。
        """
        return t.tool_run_backtest(
            strategy_file=strategy_file,
            start=start,
            end=end,
            params=params,
            benchmark=benchmark,
            execution=execution,
            initial_cash=initial_cash,
            name=name,
        )

    @mcp.tool()
    def list_experiments(kind: str | None = None, limit: int = 20) -> str:
        """列出最近的实验（kind: backtest/factor，默认全部）。"""
        return t.tool_list_experiments(kind=kind, limit=limit)

    @mcp.tool()
    def get_experiment(run_id: str) -> str:
        """查看单个实验的完整配置、指标与产物路径。"""
        return t.tool_get_experiment(run_id=run_id)

    @mcp.tool()
    def compare_experiments(run_ids: list[str]) -> str:
        """并排对比多个实验的核心指标（至少 2 个 run_id，来自 list_experiments）。"""
        return t.tool_compare_experiments(run_ids=run_ids)

    @mcp.tool()
    def run_factor_analysis(
        factor_file: str,
        universe: list[str],
        start: str,
        end: str,
        params: dict[str, Any] | None = None,
        quantiles: int = 5,
        fwd_period: int = 1,
        name: str | None = None,
    ) -> str:
        """运行因子分析：RankIC + 分层回测（自动留痕）。

        factor：因子文件绝对路径（内含一个 Factor 子类，compute 返回
        index=date/columns=symbol 的宽表）或已注册因子名（内置：Mom/
        Reversal/Volatility/Illiq/VWAPDev/AtrRatio，用 list_registered_factors
        查全量）；universe 为股票池符号列表（建议 50 只以上，截面太少
        IC 不可信）；quantiles 分层数；fwd_period 前瞻收益期。解读：
        |IC均值|>0.03 且 ICIR>0.5 才值得继续；层间单调性比多空收益更重要。
        """
        return t.tool_run_factor_analysis(
            factor_file=factor_file,
            universe=universe,
            start=start,
            end=end,
            params=params,
            quantiles=quantiles,
            fwd_period=fwd_period,
            name=name,
        )

    @mcp.tool()
    def run_vectorized_backtest(
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
        """向量化因子筛选：做多头部分位、做空尾部分位的组合净值（秒级）。

        factor 为因子文件路径或已注册因子名（见 run_factor_analysis）。
        适合批量扫参数的快速迭代；逐日再平衡、收盘成交、单一费率，
        结论仅用于相对比较，最终结论用 run_backtest 事件引擎复核。
        """
        return t.tool_run_vectorized_backtest(
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

    @mcp.tool()
    def run_backtest_sandboxed(
        strategy_file: str,
        start: str,
        end: str,
        params: dict[str, Any] | None = None,
        benchmark: str | None = "000300.SH",
        timeout: float = 300.0,
        name: str | None = None,
    ) -> str:
        """沙箱回测：子进程隔离执行（策略死循环/崩溃不影响本会话）。

        参数与 run_backtest 相同；timeout 秒后强制终止并返回 TIMEOUT。
        不确定策略质量时优先用本工具而非 run_backtest。
        """
        return t.tool_run_backtest_sandboxed(
            strategy_file=strategy_file,
            start=start,
            end=end,
            params=params,
            benchmark=benchmark,
            timeout=timeout,
            name=name,
        )

    @mcp.tool()
    def run_paper_session(
        strategy_file: str,
        name: str,
        start: str | None = None,
        end: str | None = None,
        params: dict[str, Any] | None = None,
        initial_cash: float = 1_000_000.0,
    ) -> str:
        """运行模拟盘会话（状态持久化，建议每交易日收盘后调用一次）。

        首次调用初始化组合（start 为回放起点）；之后每次调用增量处理新
        交易日：昨日订单今日开盘撮合、今日收盘产出新订单并落盘。params
        仅首次生效；name 是模拟盘的唯一标识。
        """
        return t.tool_run_paper_session(
            strategy_file=strategy_file,
            name=name,
            start=start,
            end=end,
            params=params,
            initial_cash=initial_cash,
        )

    @mcp.tool()
    def paper_status(name: str) -> str:
        """查看模拟盘当前状态：现金、持仓、待执行订单、最后处理日期。"""
        return t.tool_paper_status(name=name)

    @mcp.tool()
    def live_status() -> str:
        """查询 QMT 实盘账户资金与持仓（只读，需 cfquant 桥接在线）。

        返回资金明细与全部持仓。查询工具始终只读；下单另见 live_submit_order，
        其默认被 SOLIDROCK_LIVE_READ_ONLY 拒绝（配置默认 true）。
        """
        return t.tool_live_status()

    @mcp.tool()
    def live_orders(cancelable_only: bool = False) -> str:
        """查询实盘委托（cancelable_only=True 时只查可撤委托）。"""
        return t.tool_live_orders(cancelable_only=cancelable_only)

    @mcp.tool()
    def live_trades() -> str:
        """查询实盘成交记录。"""
        return t.tool_live_trades()

    @mcp.tool()
    def live_submit_order(
        symbol: str,
        side: str,
        qty: int,
        price: float | None = None,
        strategy_name: str = "solidrock-agent",
        confirm: bool = False,
    ) -> str:
        """向 QMT 提交实盘订单（真实资金！）.

        symbol 为统一符号（如 000001.SZ）；side: buy/sell；qty: 股数（买入须
        100 股整手）；price: None=最新价市价，给定则为限价。

        **必须先向用户复述标的/方向/数量/价格并取得同意，再以 confirm=true 调用**；
        未传 confirm=true 一律拒绝，这是代码层闸门而非提示词约定。下单还受强制
        守卫约束（只读配置、标的白名单、单笔金额上限、交易时段、幂等去重），
        任一不满足都会返回对应错误码，委托不会发出。审计日志自动记录。
        """
        return t.tool_live_submit_order(
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            strategy_name=strategy_name,
            confirm=confirm,
        )

    @mcp.tool()
    def live_cancel_order(order_id: str) -> str:
        """撤销实盘委托。"""
        return t.tool_live_cancel_order(order_id=order_id)

    @mcp.tool()
    def live_reconcile(
        paper_name: str | None = None,
        target: dict[str, int] | None = None,
        liquidate_untracked: bool = False,
    ) -> str:
        """实盘对账：对比 QMT 实际持仓与目标持仓，只输出差异与建议，不下单.

        paper_name：对比指定模拟盘的持仓；target：手工指定目标，如
        {"510300.SH": 1000}。二者选一。

        liquidate_untracked 默认 false：实盘持有但目标未包含的标的不会被建议
        清仓，而是列在 untracked 中待人工确认（避免误清实盘其他持仓）。
        """
        return t.tool_live_reconcile(
            paper_name=paper_name,
            target=target,
            liquidate_untracked=liquidate_untracked,
        )

    @mcp.tool()
    def run_ml_walk_forward(
        factor_names: list[str],
        universe: list[str],
        start: str,
        end: str,
        horizon: int = 5,
        train_window: int = 252,
        test_window: int = 21,
        step: int = 21,
    ) -> str:
        """Walk-forward ML 管道：滚动训练/预测 + 预测 IC + 向量化回测.

        factor_names 为已注册因子名列表（通过 register_factor 注册的插件因子，
        或内置因子）。universe 为股票池符号列表。返回预测 IC、特征重要性、
        向量化回测指标。过拟合检测：比较各窗口的 train_ic vs test_ic。
        """
        return t.tool_run_ml_walk_forward(
            factor_names=factor_names,
            universe=universe,
            start=start,
            end=end,
            horizon=horizon,
            train_window=train_window,
            test_window=test_window,
            step=step,
        )

    return mcp


def main() -> None:
    """stdio 入口（srq mcp serve）。"""
    server = build_server()
    server.run()  # stdio


if __name__ == "__main__":
    main()
