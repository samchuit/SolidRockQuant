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

        factor_file：因子文件绝对路径（内含一个 Factor 子类，compute 返回
        index=date/columns=symbol 的宽表）；universe 为股票池符号列表（建议
        50 只以上，截面太少 IC 不可信）；quantiles 分层数；fwd_period 前瞻
        收益期。解读：|IC均值|>0.03 且 ICIR>0.5 才值得继续；层间单调性比
        多空收益更重要。
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

    return mcp


def main() -> None:
    """stdio 入口（srq mcp serve）。"""
    server = build_server()
    server.run()  # stdio


if __name__ == "__main__":
    main()
