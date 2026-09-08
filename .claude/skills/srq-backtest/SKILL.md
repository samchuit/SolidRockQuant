---
name: srq-backtest
description: 用 SolidRockQuant 完成量化回测研究闭环。当用户要求写交易策略、跑回测、验证策略想法（A股/期货）时使用。
---

# SolidRockQuant 回测研究闭环

通过 `srq mcp serve` 暴露的 MCP 工具操作（若未连接，提示用户在 .mcp.json 配置）。

## 流程

1. **看数据**：`get_data_overview` —— 确认目标标的日线覆盖"回测起点往前 250 个交易日"（warmup）与交易日历；缺则 `fetch_bars(symbols=[...], start=<回测起点-15个月>)`。
2. **写策略**：在工作区写一个 `.py` 文件（内含一个 `Strategy` 子类）。可用 API 仅限：`ctx.order / order_value / order_target_percent / order_target_value / cancel_all / history / position / log` 与只读属性 `portfolio / now / params / universe`。数据只用截至当前 bar 的信息。
3. **校验**：`validate_strategy(strategy_file=<绝对路径>)` —— error 必须修复后再回测。
4. **回测**：`run_backtest(strategy_file, start, end, params={...}, name="<可辨识实验名>")` —— 读返回的 metrics（重点：annual_return / sharpe / max_drawdown / trade_win_rate / annual_turnover / excess_annual_return）与 rejection_counts。
5. **对比**：`list_experiments` → `compare_experiments`。单变量迭代；警惕过拟合（换区间/换标验证）。

## 要点

- 符号格式：`000001.SZ` / `510300.SH` / `000300.SH`(指数) / `RB2505.SHFE`(期货) / `RB.SHFE`(主连)。
- 所有工具返回 JSON 信封；出错先读 `error.hint` 再行动，不要重复相同参数。
- A股成本默认双边约 0.15%；高换手策略先看费用吃掉多少收益。
- 引擎默认防前视（next_open）；`same_close` 只用于快速探索，结论不可信。
