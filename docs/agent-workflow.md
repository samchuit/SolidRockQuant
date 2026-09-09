# Agent 研究闭环示例（v0.3）

本示例演示一个 AI Agent（如 Claude）如何用 MCP 工具完成"筛选 → 回测 → 模拟盘"的完整研究循环。
所有工具调用都返回统一 JSON 信封，报错自带修复 hint。

## 第 0 步：了解本地数据

```
get_data_overview          # 已缓存什么？日历覆盖到哪？
data_health                # 数据有没有坏？
```

数据不足时：`fetch_bars(symbols=["510300.SH"], start="2024-01-01")`（日线）。

## 第 1 步：因子快速筛选（秒级）

写因子文件 `momentum_20.py`，然后：

```
run_vectorized_backtest(factor_file=..., universe=[...50只...],
                        start="2024-01-01", end="2025-12-31", top=0.2, bottom=0.2)
```

看 `metrics.sharpe` 与 `annual_turnover`：夏普 < 0.5 或换手 > 1000% 的因子直接放弃，
回到因子文件改逻辑（单变量原则）。

## 第 2 步：因子显著性确认

```
run_factor_analysis(factor_file=..., universe=[...], start=..., end=..., quantiles=5)
```

判据：|IC 均值| > 0.03、ICIR > 0.5、层间单调。通过才进入事件回测。

## 第 3 步：事件回测（真实约束）

写策略文件（把因子信号转成交易规则），先 `validate_strategy`，再：

```
run_backtest(strategy_file=..., start=..., end=..., params={...}, name="v0.2-动量-周频调仓")
```

不确定策略质量（Agent 新生成的代码）时用 `run_backtest_sandboxed`——
死循环/崩溃只会终止沙箱子进程。

## 第 4 步：对比与迭代

```
list_experiments(kind="backtest", limit=20)
compare_experiments(run_ids=[...])
```

每次只改一个变量；对比基于同一数据快照；换区间复验防过拟合。

## 第 5 步：模拟盘跟踪

```
run_paper_session(strategy_file=..., name="momentum-daily", start="2025-01-01")
# 每个交易日收盘后：
run_paper_session(strategy_file=..., name="momentum-daily")   # 增量处理新交易日
paper_status(name="momentum-daily")                            # 查现金/持仓/挂单
```

模拟盘状态持久化在 `{data_dir}/paper/{name}/`，挂单自动携带到下一时段。

## 纪律清单

1. 因子筛选 → 显著性 → 事件回测 → 模拟盘，任何一步不过就停下改进，不要跳级；
2. 所有对比实验使用同一数据快照；
3. 参数扫描的"最优"必须换区间/换标的复验；
4. 向量化结论仅用于相对比较，正式结论以事件引擎为准；
5. 实盘前先模拟盘跟踪至少一个月。
