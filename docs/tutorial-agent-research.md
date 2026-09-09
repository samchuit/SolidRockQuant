# 实战教程：让 Claude 帮你做 A 股因子研究

本文是一次真实研究的完整记录：用 SolidRockQuant 的 MCP Server，让 Claude 独立完成
"数据准备 → 因子筛选 → 显著性确认 → 事件回测 → 模拟盘跟踪"的全流程。所有工具调用
都是真实发生的 MCP 协议调用，非虚构。

## 0. 环境准备（一次性）

```bash
pip install "solidrock-quant[mcp,sources]"
srq init
srq data calendar --update
```

在项目根目录创建 `.mcp.json`：

```json
{
  "mcpServers": {
    "solidrock": {
      "command": "srq",
      "args": ["mcp", "serve"],
      "env": { "SOLIDROCK_DATA_DIR": "/绝对路径/.solidrock" }
    }
  }
}
```

启动 Claude Code，`/mcp` 确认 solidrock 已连接。

## 1. 对 Agent 说目标，而不是步骤

> **你**：我想验证一下 A 股的 20 日动量因子还有没有效。股票池用沪深300成分股里
> 你挑的 30 只流动性好的，回测区间 2024-2025，先做因子分析，有效的话再回测一个
> 按它选股的策略。

Agent 的第一个动作是 `get_data_overview` 摸清本地数据，然后发现缺成分股行情，
自动调用 `fetch_bars` 批量补数据（增量幂等，重复运行安全）。**你不需要告诉它
怎么拉数据——报错信封里的 hint 会引导它自纠错。**

## 2. Agent 写因子，先过静态检查

Agent 写出 `momentum_20.py` 后，第一件事是 `validate_strategy`——不是回测：

```
validate_strategy(factor_file="momentum_20.py")
→ status: ok | issues: [] | passed: [...]
```

如果它写了 `close.shift(-1)`（前视）或调用了不存在的 API，这里就会被拦截，
error 里的 hint 直接指出怎么改。**这是给 Agent 的安全网，也是给你的护栏。**

## 3. 因子显著性：数字会说话

```
run_factor_analysis(factor_file="momentum_20.py", universe=[...30只...],
                    start="2024-01-01", end="2025-12-31", quantiles=5)
```

返回的 `ic_summary` 里看三个数：

| 指标 | 及格线 | 说明 |
|------|--------|------|
| IC 均值 | \|IC\| > 0.03 | 方向与强度 |
| ICIR | > 0.5 | 稳定性 |
| IC > 0 占比 | > 55% | 一致性 |

`layer_stats` 检查**层间单调性**——第 1 层到第 5 层收益应大致递增/递减。
跳层的因子大概率是运气。

## 4. 事件回测：真实约束下的成绩单

```
run_backtest(strategy_file="momentum_strategy.py", start=..., end=...,
             params={...}, name="动量-周频调仓-v1")
```

事件引擎与向量化筛选的区别在这里体现：**T+1、涨跌停、整手、佣金印花税滑点**
全部生效。重点关注：

- `annual_turnover` × 双边成本 ≈ 费用拖累——高换手低超额的因子白忙；
- `rejection_counts`——`WEIGHT_CAP`/`LIMIT_UP` 多说明策略与风控/市场规则打架；
- 与 `compare_experiments` 里的历史版本比，而不是与自己的最高点比。

## 5. 模拟盘：让时间做最后的裁判

```
run_paper_session(strategy_file=..., name="momentum-daily")
```

首次调用初始化组合；之后**每个交易日收盘后让 Agent（或系统计划任务）调用
一次**——昨日订单今日开盘成交、今日收盘产出新订单，状态持久化在
`.solidrock/paper/`。一个月后 `paper_status` 看模拟收益与回测是否一致，
偏离过大就回头查数据或过拟合。

## 纪律（Agent 与人类共同遵守）

1. 单变量迭代：每次实验只改一处，实验名带上改动点；
2. 同一数据快照下的对比才有效；
3. 向量化结论仅用于相对比较，正式结论以事件引擎为准；
4. 模拟盘至少跟踪一个月再谈实盘；**本项目不构成投资建议**。

---

MCP 工具完整清单见 [MCP 接入指南](mcp-setup.md)；本文每一步对应的
操作规范在 `src/solidrock/agent/skills/`。
