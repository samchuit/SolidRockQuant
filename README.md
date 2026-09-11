# SolidRockQuant · 磐石智擎

[简体中文](README.md) | [English](README.en.md)

> **Agent 原生的量化研究与回测框架** —— 让 AI Agent 像研究员一样工作，让人类像审稿人一样把关。
>
> The agent-native quant research & backtest framework for China A-shares & futures.

[![CI](https://github.com/samchuit/SolidRockQuant/actions/workflows/ci.yml/badge.svg)](https://github.com/samchuit/SolidRockQuant/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-261230.svg)](https://github.com/astral-sh/ruff)
[![MCP](https://img.shields.io/badge/MCP-23%20tools-6f42c1)](docs/mcp-setup.md)

---

## 为什么做这个项目

vnpy、qlib、backtrader、rqalpha 等优秀框架都是「给人用」的，AI Agent 接入时处处碰壁：

- 报错信息人类能猜懂，Agent 只会反复重试；
- 回测结果埋在日志里，Agent 无法可靠地读取和对比；
- 数据没有版本概念，同一策略两次回测结果不同，Agent 无法判断是策略问题还是数据漂移；
- 没有统一的工具协议，每个 Agent 平台都要单独写胶水代码。

**SolidRockQuant 从第一天起就为 LLM Agent 设计**：MCP Server 是一等公民接口，实验自动留痕，数据可版本化，报错自带修复建议。目标是让 Agent 在最少人工干预下完成「拉数据 → 写策略/因子 → 回测/分析 → 读结果 → 迭代」的完整研究闭环，而人负责设定目标与审阅结论。

覆盖市场：**中国 A 股（股票 / ETF / 指数 / 可转债）与国内期货**，境外股票经官方 `yfinance` 插件接入。

---

## 核心特性

### 🤖 Agent 原生

- **MCP Server**（`srq mcp serve`）：**23 个工具**覆盖数据、回测、因子、ML、实验、模拟盘与实盘，Claude 等 MCP 客户端即插即用；
- **结构化输出**：统一 JSON 信封（`status` / `data` / `artifacts` / `error` / `next_suggested_tools`）+ Markdown 报告；错误码稳定，**必带可执行的修复 `hint`**；
- **前视偏差检测**：回测前对策略做 AST 静态检查，拦截 `shift(-n)`、`bfill`、幻觉 API 等 LLM 高频错误；
- **确定性可复现**：引擎无随机性 + 数据版本快照，同一配置永远得到同一结果；
- **Agent 操作手册**：`src/solidrock/agent/skills/` 内置回测、数据更新、实验对比三份 playbook。

### 📊 数据层

- **可插拔数据源**：通达信（pytdx 直连，白盒 xdxr 复权）、AKShare（东财主通道 + 新浪自动回退）、Tushare Pro、Baostock；境外经官方 `yfinance` 插件；商业接口（Wind 等）可自行扩展；
- **频率与品种**：日线 + 分钟线（1m/5m）；股票、ETF/LOF、指数、可转债、期货合约与主连；
- **统一数据规范**：原始价 + 后复权因子（前复权价随除权漂移、破坏复现性，故不落库）、交易日历、统一符号（`000001.SZ` / `RB2505.SHFE`）；
- **本地缓存**：Parquet 原子写入 + DuckDB 参数化读取，增量更新幂等；
- **数据体检**（`srq data doctor`）：缺失、异常值、因子缺失、大幅波动检测。

### ⚙️ 回测引擎（自研轻量）

- **事件驱动 bar 级引擎**，默认 **`next_open` 防前视执行**（收盘出信号、次日开盘成交）；
- **A 股规则内置**：T+1（含 T+0 品种自动识别）、分板块涨跌停（主板 10% / 创业板科创 20% / 北交 30%，ST 可覆盖）、整手校验、停牌处理；
- **费用模型**：佣金（最低 5 元）/ 印花税（卖出）/ 过户费 / 滑点，全部可配置；拒单携带原因码（`LIMIT_UP` / `T_PLUS_ONE` / `WEIGHT_CAP` / `SUSPENDED` / `NO_MORE_BARS`…）；
- **期货支持**：17 个品种的合约规格（乘数 / 保证金率 / 开平仓与平今费率 / 涨跌停）、双向持仓、保证金约束、到期强平、主连换月比例复权；
- **策略钩子**：`setup` → `on_market_open` → `on_signal` → `on_market_close` → `on_stop`；分钟频可用 `trigger_times` 定时触发；
- **风控**：单标的权重上限（计入已有持仓）、回撤熔断自动清仓。

### 🔬 因子研究

- `Factor` 基类 + 面板容器（宽表）；MAD / 分位数去极值、zscore、行业市值中性化（OLS 残差）；
- **RankIC / ICIR / t 值**、分位数分层回测、多空价差、因子自相关（换手代理）；
- **内置因子**导入即注册：Mom / Reversal / Volatility / Illiq（Amihud）/ VWAPDev / AtrRatio；
- **向量化快速筛选通道**（`srq factor screen`）：因子 → 多空权重 → 净值，秒级完成数百标的扫描；内置 `lag=1` 杜绝前视。

### 🧮 指标库 / ML / 组合

- **向量化技术指标**（`solidrock.indicators`）：sma / ema / ref / diff / hhv / llv / cross / roc / bias / macd / rsi / boll / atr / kdj，对齐通达信口径，兼容 Series 与多标的宽表；
- **ML 管道**（`srq ml walk-forward`）：数据集构建 → 滚动训练 → 严格样本外预测，可选 LightGBM / scikit-learn；
- **组合优化**（`solidrock.portfolio`）。

### 📈 报告与绩效

- 指标：累计/年化收益、年化波动、夏普、索提诺、卡玛、最大回撤、Alpha/Beta、日胜率、交易胜率、盈亏比、最大连盈/连亏、单笔最大盈亏、成交笔数、总费用、年化换手、基准对比、月度收益；
- 产物：**Markdown + JSON + 单文件 HTML 交互报告**（plotly，净值/回撤、月度热力图、收益分布、滚动夏普）。

### 🧪 实验追踪（Agent 的记忆）

- 每次回测 / 因子分析自动记录：参数、配置、数据快照、指标、产物路径（SQLite，零部署）；
- `srq experiment list / show / compare` —— Agent 可回答「上次跑的动量策略和这次比哪个好」。

### 🖥️ 模拟盘与实盘

- **模拟盘**（`srq paper run`）：状态持久化的日频跟踪，复用同一套策略代码与引擎规则，跨日续跑携带持仓/挂单/复权游标/熔断状态；
- **实盘**（`srq live ...`）：QMT / cfquant 桥接，策略驱动会话（open/signal/close 三阶段），持仓对账；
- **实盘安全闸门**（详见下节）：默认只读、下单守卫、显式确认、审计留痕。

---

## 实盘安全（重要）

实盘是唯一会动用真实资金的模块，因此安全约束**在代码层强制执行，调用方无法通过传参跳过**。
`live_submit_order` 必须**同时**通过以下检查，任一不满足即拒绝，委托不会发出：

| 闸门 | 配置项 | 默认值 | 拒绝错误码 |
|------|--------|--------|-----------|
| 显式确认 | MCP 工具参数 `confirm=true` | 必须显式传入 | `PARAM_INVALID` |
| 只读模式 | `SOLIDROCK_LIVE_READ_ONLY` | **`true`（默认拒绝下单）** | `LIVE_READ_ONLY` |
| 标的准入 | `SOLIDROCK_LIVE_SYMBOL_WHITELIST` | 空（不限制，**建议配置**） | `LIVE_SYMBOL_NOT_ALLOWED` |
| 单笔金额上限 | `SOLIDROCK_LIVE_MAX_ORDER_NOTIONAL` | 500,000 元 | `LIVE_ORDER_TOO_LARGE` |
| 交易时段 | `SOLIDROCK_LIVE_ENFORCE_TRADING_HOURS` | `true`（09:15–11:30 / 13:00–15:05） | `LIVE_NOT_TRADING_HOURS` |
| 幂等去重 | `SOLIDROCK_LIVE_DUPLICATE_WINDOW_SECONDS` | 60 秒 | `LIVE_DUPLICATE_ORDER` |

补充设计：

- **本地校验先于连接**：只读 / 参数 / 守卫检查全部先于连接 QMT，被拒的订单**不会触碰券商**，也不要求桥接在线；
- **撤单不受时段限制**：撤单是降低风险的动作，任何时段放行（仍受只读约束）；
- **对账口径正确**：用总持仓（`volume`）对比、可卖量（`can_use_volume`）约束卖出，避免 A 股 T+1 下把「已持有」误判为「缺仓」而重复买入；目标未包含的实盘持仓**默认不自动清仓**；
- **审计与告警**：下单 / 撤单 / 被拦截 / 对账差异均写入 `{data_dir}/live/audit.jsonl` 并推送通知；通知失败原因落盘 `{data_dir}/live/notify_errors.log`，避免告警静默失效。

> ⚠️ 实盘涉及重大资金风险。请先在模拟盘验证策略，并从小额、白名单、低金额上限开始。

---

## 安装

```bash
# 从源码安装（PyPI 发布后可直接 pip install solidrock-quant）
pip install "solidrock-quant[mcp,sources] @ git+https://github.com/samchuit/SolidRockQuant.git"
```

可选 extras：

| Extra | 内容 |
|-------|------|
| `sources` | AKShare / Tushare / pytdx 数据源依赖 |
| `mcp` | MCP Server |
| `report` | plotly（HTML 交互报告） |
| `ml` | LightGBM / scikit-learn |
| `dev` | pytest / ruff / mypy / mkdocs |

---

## 快速开始

```bash
srq init                                                    # 初始化数据目录
srq data calendar --update                                  # 拉取交易日历
srq data update --symbols 510300.SH --start 2024-01-01      # 更新行情
```

### Python API

```python
from solidrock import BacktestConfig, BacktestEngine, Context, DataStore, Strategy


class DualMA(Strategy):
    """双均线策略：快线上穿慢线满仓，反之清仓。"""

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
# 回测（自动静态校验 + 实验留痕 + 报告落盘）
srq backtest run examples/dual_ma.py --start 2025-01-01 --end 2026-09-08 --param fast=5

# 因子研究
srq factor analyze examples/momentum_factor.py -u 000001.SZ,600519.SH --start 2025-01-01 --end 2026-09-08
srq factor screen  examples/momentum_factor.py -u ... --top 0.2 --bottom 0.2   # 向量化快筛

# ML 因子合成（滚动训练，严格样本外）
srq ml walk-forward --universe ... --start 2019-01-01 --end 2026-09-08

# 模拟盘（状态持久化，建议每日收盘后跑一次）
srq paper run examples/dual_ma.py --name my-paper
srq paper status --name my-paper

# 实验与数据版本
srq experiment list
srq experiment compare <id1> <id2>
srq data snapshot create snap-20260908

# 数据诊断
srq data doctor
srq data sources
```

### MCP Server（Agent 接入）

```json
{
  "mcpServers": {
    "solidrock": { "command": "srq", "args": ["mcp", "serve"] }
  }
}
```

接入后 Agent 即获得 **23 个工具**（数据 / 回测 / 因子 / ML / 实验 / 模拟盘 / 实盘），
完整清单与实盘安全配置见 [docs/mcp-setup.md](docs/mcp-setup.md)。

---

## 数据规范（全库唯一口径）

- **存储**：原始价 + 后复权因子（`hfq = close × adj_factor`），**不做前复权落库**——前复权价会随每次除权漂移，破坏历史复现性；
- **单位**：`volume` 为**手**，`amount` 为**元**；`pre_close` 为除权后口径；
- **日线标准列**：`symbol, date, open, high, low, close, pre_close, volume, amount, turnover_rate, adj_factor, suspended`；期货另含 `settle, open_interest`；
- **符号规范**：`000001.SZ` / `600519.SH` / `RB2505.SHFE` / 主连 `RB.SHFE`。

---

## 项目结构

```
src/solidrock/
├── data/          # 数据层：可插拔数据源、本地仓库（Parquet+DuckDB）、交易日历、符号规范、数据体检
├── backtest/      # 回测：事件驱动引擎、撮合（A股/期货规则）、组合核算、费用模型、模拟盘、向量化回测
├── strategy/      # Strategy 基类与加载器
├── factors/       # 因子：基类、截面处理、RankIC/分层分析、内置因子、报告
├── indicators.py  # 向量化技术指标库
├── ml/            # ML 管道：数据集、walk-forward、模型封装
├── portfolio/     # 组合优化
├── report/        # 绩效指标 + Markdown / JSON / HTML 报告
├── experiments/   # SQLite 实验追踪
├── risk/          # 仓位上限、回撤熔断
├── live/          # 实盘：QMT/cfquant 桥接、下单守卫、对账、策略驱动会话
├── agent/         # MCP Server、错误规范、策略静态校验、SKILL.md
├── cli/           # srq 命令行
└── utils/         # 通用工具
```

---

## 质量保障

| 检查项 | 状态 |
|--------|------|
| 单元测试 | **389 passing**（`pytest -m "not network"`） |
| 覆盖率 | **79%**（回测引擎核心模块 > 90%） |
| 类型检查 | `mypy src/solidrock` 无错误 |
| 代码风格 | `ruff check` + `ruff format --check` 全通过 |
| CI | lint + Python 3.10/3.11/3.12 测试矩阵 + CLI smoke + 文档站构建 |

提交前请运行：

```bash
ruff check src tests && ruff format --check src tests
mypy src/solidrock
pytest -m "not network"
```

---

## 文档

| 文档 | 内容 |
|------|------|
| [docs/design.md](docs/design.md) | 技术设计：架构、数据规范、引擎与 MCP 设计、关键取舍 |
| [docs/mcp-setup.md](docs/mcp-setup.md) | MCP 接入配置、工具清单、**实盘安全闸门** |
| [docs/agent-workflow.md](docs/agent-workflow.md) | Agent 研究闭环实战 playbook |
| [docs/tutorial-agent-research.md](docs/tutorial-agent-research.md) | 实战教程：让 Claude 做因子研究 |
| [docs/plugins.md](docs/plugins.md) | 数据源 / 因子插件开发指南 |
| [docs/roadmap.md](docs/roadmap.md) | 路线图与任务拆解 |
| [docs/release.md](docs/release.md) | 版本发布流程 |
| [docs/index.md](docs/index.md) | 文档站首页 |
| [文档站](https://samchuit.github.io/SolidRockQuant/) | 以上文档的在线版本 |
| [CHANGELOG.md](CHANGELOG.md) | 变更日志 |
| [llms.txt](llms.txt) | 面向 LLM 的项目速览 |

---

## 路线图

| 版本 | 内容 | 状态 |
|------|------|------|
| v0.1 | 数据层（日线）+ 事件回测（股票）+ 绩效报告 + 实验追踪 + MCP Server | **完成** |
| v0.2 | 因子分析 · 期货回测（保证金/双向持仓/换月）· 分钟线 · 数据体检 | **完成** |
| v0.3 | 模拟盘 · 策略沙箱校验 · Agent 研究闭环示例 | **完成** |
| v0.4 | 插件注册机制（`plugins/` workspace）· 英语文档 | **完成** |
| v0.5 | 分钟线回测 · 海外标的符号 · 官方 yfinance 插件 | **完成** |
| v0.6 | ML 管道 · 组合优化 · 通知 · 股票实盘（QMT / cfquant 桥接） | **完成** |
| v0.7 | 指标库 · 内置因子 · HTML 报告 · 盘前盘后钩子 · T+0 撮合 · TDX 数据源 · 实盘下单守卫 | **进行中** |
| v0.8 | 期货 CTP 实盘 · 更多数据源 · Agent 多轮研究编排 | 规划中 |

已发布版本的详细变更见 [CHANGELOG.md](CHANGELOG.md)。

---

## 参与贡献

项目处于早期快速迭代阶段，欢迎通过 Issue 讨论设计与提交 PR：

1. Fork → 创建特性分支 → 提交前运行 `ruff check` / `mypy src/solidrock` / `pytest -m "not network"`；
2. 新功能请附带测试（CI 会在 Python 3.10–3.12 上执行全量检查）；
3. **面向 Agent 的改动请同步更新 `src/solidrock/agent/skills/` 与 docstring**——它们就是 LLM 面向的文档；
4. 改动实盘相关代码时，请为新增的每条放行路径补充与安全闸门相对应的测试。

详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 免责声明

本项目仅供量化研究与技术学习使用。回测结果基于历史数据与简化假设，**不构成任何投资建议**；期货实盘与自动交易涉及重大资金风险，请遵守所在司法辖区的法律法规并自担风险。数据来源为第三方公开接口，准确性不作保证。

## 许可

[Apache-2.0](LICENSE)
