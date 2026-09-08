---
name: srq-experiment-compare
description: 用 SolidRockQuant 实验追踪对比回测结果、指导策略迭代。当用户要求对比回测、分析哪个策略好、改进参数时使用。
---

# SolidRockQuant 实验对比与迭代

## 循环

`list_experiments(limit=20)` → 挑同策略不同参数（或同区间不同策略）的 run_id → `compare_experiments(run_ids=[...])` → 读差异 → 形成假设 → **只改一个变量** → `run_backtest` → 重复。

## 指标解读（陷阱意识）

| 指标 | 含义 | 陷阱 |
|------|------|------|
| annual_return | 年化收益（几何） | 短区间会被放大 |
| sharpe | 年化/波动 | <0.5 基本不可用；平稳才可信 |
| max_drawdown | 最大回撤 | 结合年化看（卡玛=年化/\|回撤\|） |
| trade_win_rate | 平仓胜率 | 趋势策略 30-40% 是常态 |
| profit_factor | 总盈利/总亏损 | >1.3 才值得继续优化 |
| annual_turnover | 年化换手 | 高换手+低超额 = 费用吃掉收益 |
| excess_annual_return | 相对基准年化 | 评估策略的最终标准 |

## 纪律

1. 对比的实验必须基于同一数据快照（`get_experiment` 看 data_snapshot）；
2. 实验名带改动点（如 `双均线-fast10`）；
3. 防过拟合：最优参数换 2-3 个不重叠区间、1-2 个同类标的复验；邻居参数不应断崖式变差；
4. 深入分析读 `get_experiment(run_id).artifacts_dir` 下的 result.json（逐日净值/全部成交/拒单）。
