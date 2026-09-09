# MCP Server 接入指南

SolidRockQuant 通过 MCP（Model Context Protocol）把数据、回测、实验追踪暴露给任意 LLM 客户端。传输为 stdio（本地进程），HTTP/SSE 计划在后续版本。

## 前置条件

```bash
pip install "solidrock-quant[mcp,sources]"
srq init                    # 初始化数据目录
srq data calendar --update  # 拉取交易日历
```

## Claude Code

在项目根目录创建 `.mcp.json`：

```json
{
  "mcpServers": {
    "solidrock": {
      "command": "srq",
      "args": ["mcp", "serve"],
      "env": {
        "SOLIDROCK_DATA_DIR": "/绝对路径/.solidrock",
        "SOLIDROCK_TUSHARE_TOKEN": "可选：tushare token"
      }
    }
  }
}
```

重启 Claude Code 后，用 `/mcp` 确认 solidrock 已连接。

## Claude Desktop

编辑 `claude_desktop_config.json`（设置 → 开发者 → 编辑配置），加入同样的 `mcpServers.solidrock` 段。

## 直接调试

```bash
srq mcp serve                # stdio 模式，接入真实客户端
```

## 工具清单

| 工具 | 作用 |
|------|------|
| `get_data_overview` | 本地数据总览（行情/日历/快照/实验） |
| `data_health` | 数据体检（缺失/异常/因子缺失/大幅波动） |
| `list_data_sources` | 可用数据源与能力 |
| `search_instruments` | 按代码/名称搜标的 |
| `fetch_bars` | 拉取/更新行情（1d/1m/5m，增量幂等） |
| `get_trading_calendar` | 查询/更新交易日历 |
| `validate_strategy` | 策略静态检查（前视偏差/API 误用） |
| `run_backtest` | 运行回测（自动校验 + 留痕） |
| `run_backtest_sandboxed` | 沙箱回测（子进程隔离，坏策略不影响会话） |
| `run_paper_session` / `paper_status` | 模拟盘：状态持久化的日频跟踪 |
| `run_factor_analysis` | 因子分析（RankIC + 分层） |
| `run_vectorized_backtest` | 向量化因子筛选（快速净值） |
| `list_experiments` / `get_experiment` / `compare_experiments` | 实验追踪查询与对比 |

## 返回信封

所有工具返回 JSON 字符串：

```json
{
  "status": "ok",
  "data": { "...紧凑摘要..." },
  "artifacts": ["/abs/path/report.md"],
  "next_suggested_tools": ["get_experiment:<run_id>"]
}
```

失败时 `status="error"`，`error` 段含稳定错误码与可执行 `hint`——Agent 应按 hint 自纠错，而不是重试相同参数。

## 给 Agent 的操作建议

把 `src/solidrock/agent/skills/` 下的三个技能文件加入 Agent 的技能库（Claude Code 可放 `.claude/skills/`）：

- `backtest-workflow.md`：完整回测研究闭环
- `data-update-workflow.md`：数据更新与快照
- `experiment-comparison-workflow.md`：实验对比与迭代纪律

典型指令示例：「用 510300.SH 从 2024 年开始的数据，写一个双均线策略并回测，然后和基准对比」——Agent 应依次调用 `get_data_overview` → `fetch_bars` → 写策略文件 → `validate_strategy` → `run_backtest` → 读指标 → 汇报结论。
