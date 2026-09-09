# SolidRockQuant · 磐石智擎

[简体中文](README.md) | [English](README.en.md)

> **Agent 原生的量化研究与回测框架** —— 让 AI Agent 像研究员一样工作，让人类像审稿人一样把关。
>
> The agent-native quant research & backtest framework for China A-shares & futures.

[![CI](https://github.com/samchuit/SolidRockQuant/actions/workflows/ci.yml/badge.svg)](https://github.com/samchuit/SolidRockQuant/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-261230.svg)](https://github.com/astral-sh/ruff)

## 为什么做这个项目

vnpy、qlib、backtrader、rqalpha 等优秀框架都是"给人用"的，AI Agent 接入它们时处处碰壁：

- 报错信息人类能猜懂，Agent 只会反复重试；
- 回测结果埋在日志里，Agent 无法可靠地读取和对比；
- 数据没有版本概念，同一个策略两次回测结果不同，Agent 无法判断是策略问题还是数据漂移；
- 没有统一的工具协议，每个 Agent 平台都要单独写胶水代码。

SolidRockQuant 从第一天起就为 LLM Agent 设计：**MCP Server 是一等公民接口，实验自动留痕，数据可版本化，报错自带修复建议**。目标是让 Agent 在最少人工干预下完成"拉数据 → 写策略/因子 → 回测/分析 → 读结果 → 迭代"的完整研究闭环，而人负责设定目标和审阅结论。

## 核心特性

### 🤖 Agent 原生
- **MCP Server**（`srq mcp serve`）：数据获取、回测、因子分析、实验查询共 13 个工具，Claude 等 LLM 客户端即插即用
- **结构化输出**：统一 JSON 信封 + Markdown 报告；错误码稳定且必带可执行的修复 hint
- **前视偏差检测**：`shift(-n)`、`bfill`、幻觉 API 等 LLM 常见错误的 AST 静态检查，回测前自动拦截
- **确定性可复现**：无随机性引擎 + 数据版本快照，同一实验永远得到同一结果

### 📊 数据层
- 可插拔数据源：AKShare（东财主通道 + 新浪自动回退）、Tushare Pro、Baostock，架构预留 Wind 等商业接口；日线 + 分钟线（1m/5m）
- 统一数据规范：原始价 + 后复权因子（前复权价会随除权漂移，破坏复现性，故不落库）、交易日历、股票/期货合约/主连/指数/ETF
- 本地缓存：Parquet 原子写入 + DuckDB 参数化读取，增量更新幂等

### ⚙️ 回测引擎（自研轻量）
- 事件驱动 bar 级引擎，默认 **next_open 防前视执行**（收盘信号次日开盘成交）
- A股规则内置：T+1、涨跌停按板块自动判别（主板 10% / 创业板科创 20% / 北交 30%，ST 可覆盖）、买入整手、停牌处理
- 费用模型：佣金（最低 5 元）/ 印花税（卖出）/ 过户费 / 滑点，全部可配置；拒单带原因码（`LIMIT_UP` / `T_PLUS_ONE` / `WEIGHT_CAP` / `SUSPENDED`…）
- 风控：单标的权重上限、回撤熔断自动清仓

### 🔬 因子研究
- `Factor` 基类 + 面板容器（宽表），MAD/分位数去极值、zscore、行业市值中性化（OLS 残差）
- **RankIC / ICIR / t 值**、分位数分层回测、多空价差、因子自相关（换手代理）
- 分析结果自动留痕，与回测共用同一套实验追踪

### 🧪 实验追踪（Agent 迭代的记忆）
- 每次回测/因子分析自动记录：参数、配置、数据快照、指标、产物路径（SQLite 零部署）
- Agent 可查询对比历史实验："上次跑的动量策略和这次比哪个好？"

## 快速开始

```bash
# 从源码安装（PyPI 发布后可直接 pip install solidrock-quant）
pip install "solidrock-quant[mcp,sources] @ git+https://github.com/samchuit/SolidRockQuant.git"

srq init                                      # 初始化数据目录
srq data calendar --update                    # 拉取交易日历
srq data update --symbols 510300.SH --start 2024-01-01   # 更新行情
```

### Python API 回测

```python
from solidrock import BacktestConfig, BacktestEngine, Context, DataStore, Strategy

class DualMA(Strategy):
    """双均线策略"""
    params = {"fast": 5, "slow": 20}

    def setup(self, ctx: Context):
        ctx.universe = ["510300.SH"]

    def on_signal(self, ctx: Context):
        close = ctx.history("510300.SH", self.params["slow"] + 1, fields="close")["510300.SH"]
        window = close.iloc[-self.params["slow"]:]
        if window.isna().any() or len(window) < self.params["slow"]:
            return
        target = 1.0 if window.iloc[-self.params["fast"]:].mean() > window.mean() else 0.0
        ctx.order_target_percent("510300.SH", target)

store = DataStore(".solidrock")
result = BacktestEngine(DualMA, BacktestConfig(start="2025-01-01", end="2026-09-08"), store).run()
print(result.metrics["sharpe"], result.metrics["max_drawdown"])
```

### CLI

```bash
srq backtest run examples/dual_ma.py --start 2025-01-01 --end 2026-09-08 --param fast=5
srq factor analyze examples/momentum_factor.py -u 000001.SZ,600519.SH,... --start 2025-01-01 --end 2026-09-08
srq experiment list            # 每次回测/因子分析自动留痕
srq experiment compare <id1> <id2>
srq data snapshot create snap-20260908    # 数据版本快照（实验复现）
```

### MCP Server（Agent 接入）

```json
{
  "mcpServers": {
    "solidrock": { "command": "srq", "args": ["mcp", "serve"] }
  }
}
```

Claude 等 Agent 即可获得 16 个工具，研究闭环无需人工干预。详细配置见 [docs/mcp-setup.md](docs/mcp-setup.md)，Agent 操作指南见 `src/solidrock/agent/skills/`。

## 项目结构

```
src/solidrock/
├── data/          # 数据层：数据源适配器（可插拔）、本地仓库、交易日历、符号规范
├── backtest/      # 回测引擎：事件驱动、撮合（A股规则）、组合核算、费用模型
├── strategy/      # Strategy 基类与加载器
├── factors/       # 因子研究：基类、截面处理、RankIC/分层分析
├── report/        # 绩效指标、Markdown/JSON 报告
├── experiments/   # SQLite 实验追踪
├── risk/          # 仓位上限、回撤熔断
├── agent/         # MCP Server、错误规范、策略校验、SKILL.md
├── cli/           # srq 命令行
└── utils/         # 通用工具
```

## 文档

| 文档 | 内容 |
|------|------|
| [docs/design.md](docs/design.md) | 技术设计：架构、数据规范、引擎与 MCP 设计、关键取舍 |
| [docs/roadmap.md](docs/roadmap.md) | 路线图与任务拆解 |
| [docs/mcp-setup.md](docs/mcp-setup.md) | Claude Code / Claude Desktop 接入配置 |
| [docs/plugins.md](docs/plugins.md) | 数据源/因子插件开发指南 |
| [docs/agent-workflow.md](docs/agent-workflow.md) | Agent 研究闭环实战 playbook |
| [docs/tutorial-agent-research.md](docs/tutorial-agent-research.md) | 实战教程：让 Claude 做因子研究 |
| [文档站](https://samchuit.github.io/SolidRockQuant/) | 全部文档的在线版本 |
| [docs/release.md](docs/release.md) | 版本发布流程 |
| [llms.txt](llms.txt) | 面向 LLM 的项目速览 |

## 路线图

| 版本 | 内容 | 状态 |
|------|------|------|
| v0.1 | 数据层（日线）+ 事件回测（股票）+ 绩效报告 + 实验追踪 + MCP Server | **完成** |
| v0.2 | 因子分析 ✅ · 期货回测（保证金/双向持仓/换月）· 分钟线 · 数据体检 | 进行中 |
| v0.3 | 模拟盘、期货 CTP 实盘、策略沙箱校验、多 Agent 工作流示例 | 规划中 |
| v0.4 | 插件注册机制、英语文档、更多数据源 | 规划中 |

## 参与贡献

项目处于早期快速迭代阶段，欢迎通过 Issue 讨论设计与提交 PR：

1. Fork → 创建特性分支 → 提交前运行 `ruff check` / `mypy src/solidrock` / `pytest -m "not network"`；
2. 新功能请附带测试（CI 会在 Python 3.10–3.12 上执行全量检查）；
3. 面向 Agent 的改动请同步更新 `src/solidrock/agent/skills/` 与 docstring。

## 免责声明

本项目仅供量化研究与技术学习使用。回测结果基于历史数据与简化假设，**不构成任何投资建议**；期货实盘与自动交易涉及重大资金风险，请遵守所在 jurisdictions 的法律法规并自担风险。数据来源为第三方公开接口，准确性不作保证。
