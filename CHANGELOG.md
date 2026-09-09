# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

（v0.7 规划：策略驱动实盘会话、期货实盘需 CTP 环境）

## [0.6.0] - 2026-09-09

### 新增

- **股票实盘接入（QMT / cfquant 桥接）**：`solidrock/live/` 模块——
  - `CfquantBroker`：查资金/持仓/委托/成交 + 下单/撤单（xtquant 兼容 API）；
  - 下单安全：默认只读模式（`SOLIDROCK_LIVE_READ_ONLY=true`），买入整手校验，
    side/价格/数量参数校验，审计日志（`{data_dir}/live/audit.jsonl`）；
  - **实盘对账**：`diff_positions` 目标持仓 vs QMT 实际持仓差异报告，
    `srq live reconcile` 支持 `--paper` 对比模拟盘或 `--target` 手工指定；
  - CLI `srq live status/orders/trades/order/cancel/reconcile`（下单带交互确认 + dry-run）；
  - MCP `live_status/live_orders/live_trades/live_submit_order/live_cancel_order/live_reconcile`
    （工具总数 22）
- **海外标的符号体系**（提前至 0.6.0）：`AAPL.NASDAQ` / `7203.TSE` 等格式
- **官方 yfinance 插件**：plugins/solidrock-yfinance（entry-points 接入的参考实现）

## [0.5.0] - 2026-09-09

### 新增

- **分钟线回测**：`BacktestConfig(freq="1m"/"5m")`——引擎日内时钟（bar 时间戳并集）、
  换日解锁 T+1 与平今计数、指标按日重采样保证年化口径；期货+分钟组合明确拒绝
- **海外标的符号体系**：`AAPL.NASDAQ` / `7203.TSE` 等格式（NYSE/NASDAQ/AMEX/HKEX/TSE/LSE），
  `infer_asset_type` 归类为股票
- **官方 yfinance 插件**：plugins/solidrock-yfinance（entry-points 接入的参考实现，
  原始价+后复权因子双取数推导），uv workspace 管理

## [0.4.0] - 2026-09-09

### 新增

- **模拟盘**：`PaperTrader` 状态持久化（现金/持仓/待执行订单/复权因子游标 JSON 落盘），
  增量日频运行，复用事件引擎全部规则；引擎新增 `initial_portfolio/initial_pending/
  initial_last_factors` 注入与 `carry_pending` 挂单携带；CLI `srq paper run/status`、
  MCP `run_paper_session/paper_status`
- **策略沙箱**：子进程隔离执行回测，超时击杀（`TIMEOUT` 错误码），策略 `sys.exit`/
  死循环不影响宿主；MCP `run_backtest_sandboxed`
- **Agent 研究闭环示例**：docs/agent-workflow.md（筛选→显著性→回测→对比→模拟盘）
- **插件机制**：第三方包通过 entry-points 接入数据源（组 `solidrock.sources`）
  与因子（组 `solidrock.factors`）；发现惰性触发、单个插件加载失败不拖垮框架；
  因子注册表（`register_factor`/`create_factor`/`list_registered_factors`）；
  文档 docs/plugins.md
- **英语文档**：README.en.md 完整英文版 + 双语切换链接

### 修复

- 引擎 `final_pending` 仅在 `carry_pending` 模式输出（回测模式下此前被错误填充）

## [0.2.1] - 2026-09-09

### 新增

- 社区基础设施：CONTRIBUTING.md、Bug/功能 Issue 模板、PR 模板
- `srq data peek --freq`：查看分钟线样本
- 包标记 PEP 561 `py.typed`（下游 mypy 可用类型提示）；`__version__` 改为
  从包元数据单一来源读取

### 修复

- 分钟频率（1m/5m）增量更新起点错误地跳到次日，导致当日后段 bar 拉不到；
  现从最后 bar 所在日零点起重拉（按 (symbol, date) 去重保证幂等）

## [0.2.0] - 2026-09-08

首个公开版本：v0.1 全部模块 + v0.2 期货/因子/分钟线。

### 数据层

- 可插拔数据源（AKShare 东财主通道 + 新浪自动回退、Tushare Pro、Baostock）、
  统一符号规范（`000001.SZ` / `RB2505.SHFE` / 主连 `RB.SHFE`）、标准 schema
  （原始价 + 后复权因子）、Parquet + DuckDB 本地仓库（原子写入、增量幂等）、
  数据版本快照（只读、硬链接近零拷贝）、交易日历、分钟线（1m/5m）

### 回测引擎

- bar 级事件驱动，next_open 防前视默认执行；A股规则（T+1、涨跌停分板块、整手、
  停牌）；费用模型（佣金/印花税/过户费/滑点）；拒单原因码；仓位权重上限与回撤熔断；
  公司行为的价值守恒持仓调整
- 期货：合约规格表（乘数/保证金/开平今费率/涨跌停，17 品种可覆盖）、双向持仓、
  保证金约束、平今拆分费率、到期强平、主连换月比例复权工具

### 因子研究

- Factor/FactorData 基座、MAD/分位数去极值、zscore、行业市值中性化（OLS 残差）、
  RankIC/ICIR/t 值、分位数分层回测、多空价差、因子自相关；
  向量化快速筛选通道（多空权重→净值，lag=1 防前视）

### 报告与实验追踪

- Markdown + JSON 双产物、SQLite 实验库（list/get/compare）、
  回测与因子分析自动留痕并绑定数据快照

### Agent 接口层

- MCP Server（mcp 1.x/2.x 兼容，统一 JSON 信封、错误必带 hint）、
  策略 AST 静态校验（前视偏差/幻觉 API）、SKILL.md、llms.txt

### 工程化

- uv + ruff + mypy + pytest、GitHub Actions（CI 3.10–3.12 矩阵 + 发布工作流）、
  CLI（`srq init/data/backtest/factor/experiment/mcp`）、示例策略与因子
