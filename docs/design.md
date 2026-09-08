# SolidRockQuant 技术设计

> 版本：v0.1 草案 · 2026-09-08
> 定位：Agent 原生的量化研究与回测框架。一期范围：数据层 + 事件回测（股票）+ 因子基础 + 实验追踪 + MCP Server；实盘交易放二期。

## 1. 设计原则

1. **Agent 优先**：所有 API 的第一读者是 LLM。报错必须可自修复、输出必须可机器解析、行为必须确定可复现。
2. **token 经济**：MCP 工具返回紧凑摘要 + 文件路径引用，不把大 DataFrame 塞给模型。
3. **正确性默认值**：引擎默认配置防前视（次日开盘成交），用户显式选择才放宽。
4. **薄核心、厚扩展**：核心只做数据/回测/报告，数据源、费用模型、风控规则全部可插拔。
5. **A股/期货规则是一等公民**：T+1、涨跌停、保证金、平今仓不是可选项，是内置默认。

## 2. 总体架构

```
┌─────────────────────────────────────────────────────┐
│              Agent 接口层  agent/                    │
│   MCP Server · SKILL.md · 报错规范 · 沙箱校验        │
├──────────────┬──────────────┬───────────────────────┤
│  数据层 data/ │ 回测 backtest/│ 研究 factors/         │
│  适配器·缓存  │  事件驱动引擎  │  因子定义·IC·分层      │
│  版本快照     │  撮合·费用     │  中性化·处理          │
├──────────────┴──────┬───────┴───────────────────────┤
│  报告 report/        │  实验追踪 experiments/         │
│  metrics·md·json     │  sqlite · 参数/指标/数据版本   │
├─────────────────────┴───────────────────────────────┤
│  CLI (typer)  ·  Python API  ·  config (pydantic)    │
└─────────────────────────────────────────────────────┘
```

## 3. 目录结构

```
SolidRockQuant/
├── pyproject.toml            # uv + ruff + mypy + pytest
├── src/solidrock/
│   ├── data/
│   │   ├── schema.py         # 标准表结构（pydantic 模型 + 校验器）
│   │   ├── sources/
│   │   │   ├── base.py       # DataSource 抽象基类 + 注册表
│   │   │   ├── akshare.py
│   │   │   ├── tushare.py
│   │   │   └── baostock.py   # v0.2
│   │   ├── calendar.py       # 交易日历
│   │   ├── store.py          # Parquet + DuckDB，增量更新，快照 tag
│   │   └── quality.py        # 数据体检（v0.2）
│   ├── backtest/
│   │   ├── engine.py         # 事件驱动主循环
│   │   ├── context.py        # 策略上下文 API
│   │   ├── matching.py       # 撮合：涨跌停/T+1/滑点
│   │   ├── costs.py          # 费用模型（A股/期货可插拔）
│   │   ├── portfolio.py      # 现金+持仓核算
│   │   └── futures.py        # 保证金/双向持仓/换月（v0.2）
│   ├── strategy/
│   │   ├── base.py           # Strategy 基类与生命周期
│   │   └── examples/         # 示例策略（也是文档）
│   ├── factors/
│   │   ├── base.py           # Factor 定义 API
│   │   ├── processing.py     # 去极值/标准化/中性化
│   │   └── analysis.py       # IC/IR/分层回测（v0.2 完整版）
│   ├── risk/checks.py        # 仓位上限/回撤熔断（简版）
│   ├── report/
│   │   ├── metrics.py        # 绩效指标
│   │   ├── markdown.py       # Markdown 报告
│   │   └── json_report.py    # 结构化 JSON
│   ├── experiments/tracker.py# sqlite 实验追踪
│   ├── agent/
│   │   ├── mcp_server.py     # FastMCP Server
│   │   ├── tools.py          # tool 定义（薄封装，逻辑在领域层）
│   │   ├── errors.py         # 错误码 + 修复建议规范
│   │   └── skills/           # SKILL.md 操作指南
│   ├── cli/main.py           # typer CLI
│   └── config.py             # pydantic-settings 全局配置
├── examples/                 # 可直接运行的策略与 notebook
├── tests/
└── docs/
```

## 4. 数据层设计

### 4.1 符号约定（全库统一）

| 品种 | 格式 | 示例 |
|------|------|------|
| A股 | `代码.SZ/.SH/.BJ` | `000001.SZ`、`600519.SH` |
| 指数/ETF | 同股票 | `000300.SH`、`510300.SH` |
| 期货合约 | `品种+月份.交易所` | `RB2505.SHFE`、`IF2412.CFE` |
| 期货主连 | `品种.交易所` | `RB.SHFE` |

采用 Tushare/Wind 式后缀约定（国内事实标准），所有对外接口只认这一种格式，适配器内部负责与源格式互转。

### 4.2 标准 schema

日线核心表（列名全库唯一，禁止适配器私自改名）：

```
symbol, date, open, high, low, close, pre_close,
volume, amount, turnover_rate, adj_factor, suspended(停牌)
```

**关键决策：存原始价 + 复权因子，用时计算复权**，不做前复权落库——前复权价随每次除权变动，会破坏数据版本一致性（这是很多数据源的坑）。

### 4.3 数据源适配器

```python
class DataSource(ABC):
    name: str
    capabilities: set[Capability]   # BARS_DAILY, FINANCIALS, FUTURES_CONTRACTS...

    @abstractmethod
    def fetch_bars(self, symbols, start, end, freq="1d") -> pd.DataFrame: ...
    def fetch_calendar(self, start, end) -> pd.DataFrame: ...       # 可选实现
    def resolve_symbol(self, raw) -> str: ...                        # 源格式互转
```

- 注册表模式：`@register_source("akshare")`，`create_source("tushare", token=...)`
- 一期实现 AKShare（默认，零门槛）+ Tushare Pro（质量更高，需 token）；Baostock v0.2
- 期货行情：AKShare 覆盖日线；TqSdk 留扩展点

### 4.4 本地存储与版本

- Parquet 按 `freq/symbol` 分区落盘，DuckDB 作为查询层（本地零部署）
- 增量更新：按本地最后日期续拉，幂等
- **数据快照**：`store.snapshot("snap-20260908")` 生成不可变版本 tag；实验记录绑定快照名。复现实验 = 指定快照重跑
- 数据体检（v0.2）：缺失段落、OHLC 逻辑错误、复权因子跳变、停牌日有价 → 生成健康报告

## 5. 回测引擎设计（自研轻量）

### 5.1 主循环

```
for each trading_day:
    1. 市场数据推进（当日 bar 就绪）
    2. before_trade 钩子          # 风控检查：仓位上限/回撤熔断
    3. 触发调度事件 → on_signal    # 策略产生订单意图
    4. 撮合引擎                    # 涨跌停/T+1/滑点/费用
    5. 组合核算（现金、持仓、净值）
on_stop → 绩效报告（metrics + markdown + json）→ 实验入库
```

bar 级事件驱动，numpy 向量化持仓核算；tick 与分钟级通过 `Clock` 抽象预留。

### 5.2 执行模型（防前视是默认值）

| 模式 | 行为 | 用途 |
|------|------|------|
| `next_open`（默认） | 收盘出信号 → 次日开盘价成交 | 正确性优先 |
| `same_close` | 当日收盘出信号当日收盘成交 | 快速研究，用户显式开启并在报告中标注 |

撮合限制（A股，可配置）：涨停不可买入、跌停不可卖出、T+1（当日买入不可卖）、停牌不可交易。涨跌停幅度按板块默认：主板 10%、创业板/科创板 20%、ST 5%、北交所 30%。

### 5.3 费用与滑点

可插拔 `CostModel`，默认参数（均可覆盖，费率随政策变动，只给合理默认值）：

- 佣金：万 2.5，最低 5 元；印花税：卖出 0.05%；过户费：万 0.1
- 期货（v0.2）：保证金比例、开仓/平仓/平今费率分开配置、合约乘数
- 滑点：固定 bps 或按成交量比例

### 5.4 策略 API

```python
class Strategy:
    params: dict                          # 类级默认，实例可覆盖
    def setup(self, ctx): ...             # 定义 universe、调度、参数
    def on_signal(self, ctx): ...         # 核心：产出订单意图
    def on_stop(self, ctx): ...           # 收尾
```

订单接口：`order(symbol, qty)`、`order_target_percent(symbol, pct)`、`order_target_value`、`cancel_all()`。
查询接口：`ctx.portfolio`（现金/持仓/净值）、`ctx.history(symbol, n, fields)`、`ctx.now`。
调度：`ctx.schedule(name, freq="daily", at="close"|"open")`。

### 5.5 期货扩展（v0.2）

双向持仓（多/空头寸分开核算）、保证金占用与强平线、平今仓费率、主连换月（换月日映射 + 后复权拼接，比例复权法保证收益率连续）、到期交割处理。

## 6. 因子研究（一期基础，v0.2 完整）

```python
class Momentum20(Factor):
    """20日动量"""
    def compute(self, data) -> pd.DataFrame:   # index=date, columns=symbol
        return data.close / data.close.shift(20) - 1
```

- `processing`：winsorize（MAD/分位数）、zscore、行业/市值中性化（回归取残差）
- `analysis`：IC/RankIC 序列与衰减、分位数分层净值、换手率；输出类 Alphalens 报告（Markdown + 图）
- 与回测共用同一数据层与报告基建

## 7. 实验追踪（Agent 迭代的关键基建）

SQLite 单文件，零部署。每次回测/因子分析自动写入：

```
experiments(
  id, created_at, name, kind,            # backtest | factor
  data_snapshot,                          # 绑定的数据版本 tag
  config_json,                            # 策略参数+引擎配置（完整可复现）
  metrics_json,                           # 年化/夏普/回撤等核心指标
  artifacts,                              # 报告/交易明细/净值曲线文件路径
  git_commit, seed, notes
)
```

查询 API：`list_experiments(filter)`、`get_experiment(id)`、`compare(ids)`（并排对比指标表）。**没有这个模块，Agent 无法回答"这次改动是否更好"，自主迭代就不成立。**

## 8. Agent 接口层

### 8.1 MCP 工具清单（v0.1）

| 工具 | 说明 |
|------|------|
| `search_instruments` | 按代码/名称搜股票、指数、ETF、期货合约 |
| `fetch_bars` | 拉取/更新行情，返回摘要统计 + 本地缓存路径 |
| `get_trading_calendar` | 交易日历查询 |
| `run_backtest` | 运行策略（内置策略名或代码文件），返回指标 JSON + 报告路径 |
| `run_factor_analysis` | 因子表达式 → IC/分层摘要 |
| `list_experiments` / `get_experiment` / `compare_experiments` | 实验查询与对比 |
| `validate_strategy` | 静态检查策略代码（语法、前视偏差、API 误用） |

### 8.2 输出信封规范

所有工具统一返回：

```json
{
  "status": "ok | error",
  "data": { "...紧凑摘要..." },
  "artifacts": ["/abs/path/to/report.md"],
  "error": { "code": "SYMBOL_NOT_FOUND",
             "message": "代码 1 不存在",
             "hint": "股票代码需带交易所后缀，如 000001.SZ；可先调用 search_instruments" },
  "next_suggested_tools": ["get_experiment:exp_42"]
}
```

- 摘要优先，明细落文件给 `artifacts` 引用，避免 token 爆炸
- 错误码枚举固定（`SYMBOL_NOT_FOUND`、`NO_DATA`、`LOOKAHEAD_SUSPECTED`…），hint 必须给出可执行的修复动作
- 传输：一期 stdio（Claude Desktop / Claude Code 本地接入），HTTP/SSE 二期

### 8.3 沙箱校验与技能文件

- `validate_strategy`：AST 解析策略代码，检测经典前视模式（`shift(-n)`、用未来 bar 的 close 计算当日信号、参数引用未来数据）、未定义 API 调用
- `agent/skills/` 内置 SKILL.md：常见任务的操作指南（拉数据做回测、因子分析流程、实验对比流程），随包分发，供各类 Agent 平台加载

## 9. 报告

- `metrics.py`：累计/年化收益、波动、夏普、最大回撤、卡玛、胜率、盈亏比、换手率、相对基准超额
- 回测产物：`report.md`（人读）、`result.json`（机器读）、`trades.csv`、`nav.csv`
- 因子产物：`factor_report.md`、`ic.csv`、`layer_nav.csv`

## 10. 工程化

- **Python ≥ 3.10**；核心依赖保持精简：pandas、numpy、pydantic、duckdb、pyarrow、typer、rich
- extras：`[sources]` akshare/tushare/baostock，`[mcp]` mcp SDK，`[dev]` 全套
- 质量：ruff（lint+format）、mypy（strict 渐进启用）、pytest + 覆盖率、GitHub Actions（lint/type/test/发布）
- 文档：mkdocs-material，中英双语（英文 v0.4），examples 全部可运行并在 CI 中冒烟测试
- License：Apache-2.0（含专利授权，机构友好；比 MIT 更适合基础设施类开源）

## 11. 关键取舍记录

| 决策 | 选择 | 理由 |
|------|------|------|
| 引擎 | 自研轻量 | 完全掌控 API 形态与 A股/期货规则，对 Agent 最友好；bar 级事件引擎体量可控 |
| 复权 | 存原始价+因子 | 前复权价随除权漂移，破坏数据版本复现 |
| 默认成交 | 次日开盘 | 防前视是正确性底线；放宽需显式选择 |
| 存储 | Parquet + DuckDB | 零部署、列式高效、DuckDB SQL 查询便利 |
| 实验库 | SQLite | 单文件零部署，够用；后续可换 duckdb/远端 |
| MCP 传输 | stdio 一期 | 本地 Agent 场景最简，HTTP/SSE 二期 |
| 实盘 | 二期 | 一期聚焦研究闭环，CTP 联调依赖环境，避免拖慢 MVP |
