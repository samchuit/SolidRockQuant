# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增（v0.3）

- **策略沙箱**：子进程隔离执行回测，超时击杀（`TIMEOUT` 错误码），策略 `sys.exit`/死循环不影响宿主；MCP `run_backtest_sandboxed`
- **模拟盘**：`PaperTrader` 状态持久化（现金/持仓/待执行订单/复权因子游标 JSON 落盘），增量日频运行，复用事件引擎全部规则；引擎新增 `initial_portfolio/initial_pending/initial_last_factors` 注入与 `carry_pending` 挂单携带；CLI `srq paper run/status`、MCP `run_paper_session/paper_status`
- **Agent 研究闭环示例**：examples/agent-workflow.md

### 修复

- 引擎 `final_pending` 仅在 `carry_pending` 模式输出（回测模式下此前被错误填充）

## [0.2.0] - 2026-09-08

首个公开版本：v0.1 全部模块 + v0.2 期货/因子/分钟线。

### 新增（v0.2）

- **分钟线支持**：`1m`/`5m` 频率贯通 schema/存储/适配器/CLI/MCP——AKShare
  （东财分钟 + 新浪个股回退 + 新浪期货分钟）、Tushare（stk_mins，需高积分）、
  Baostock（5 分钟）；分钟数据仅供研究（事件回测仍为日频）
- **Baostock 适配器**：A股日线（原始+后复权双取数推导因子）、交易日历、
  沪深300/上证50/中证500 成分股、5 分钟线
- **向量化回测通道**：`vectorized_backtest`（因子值→多空权重→净值，逐日
  再平衡、内置 lag=1 防前视、换手×费率计成本）+ `weights_from_factor`；
  CLI `srq factor screen`、MCP `run_vectorized_backtest`（自动留痕）
- **期货指标语义**：交易记录新增 `closing` 字段区分开/平，胜率/盈亏比只按
  平仓方向计算（修复期货开空被计为亏损导致的胜率失真）

### 新增（v0.2 期货与数据体检）

- **期货回测支持**：合约规格表（乘数/保证金/开平今费率/涨跌停幅度，覆盖 17 个常见品种，
  可按品种或完整符号覆盖）；带符号双向持仓（开/平/翻转一次成交）；保证金约束
  （可用空间 = 权益 - 占用 + 平仓释放）；平今拆分费率（当日开仓部分按平今计价）；
  到期强平（商品近似交割月 15 日、金融期货第三个周五，此后策略订单收到 `EXPIRED` 拒单）；
  主连换月比例复权工具 `roll_adjust_continuous`；示例 `examples/futures_short.py`
- **数据体检**：`srq data doctor` 与 MCP `data_health`——缺失交易日、OHLC 异常、
  复权因子缺失占比、零成交连段、单日大幅波动告警

### 新增（v0.1 基础）

v0.1 全部模块 + v0.2 因子研究模块。

### 新增

- **数据层**：可插拔数据源（AKShare 东财主通道 + 新浪自动回退、Tushare Pro）、统一符号规范
  （`000001.SZ` / `RB2505.SHFE` / 主连 `RB.SHFE`）、标准 schema（原始价 + 后复权因子）、
  Parquet + DuckDB 本地仓库（原子写入、增量幂等）、数据版本快照（只读、硬链接近零拷贝）、交易日历
- **回测引擎**：bar 级事件驱动，next_open 防前视默认执行；A股规则（T+1、涨跌停分板块、整手、
  停牌）；费用模型（佣金/印花税/过户费/滑点）；拒单原因码；仓位权重上限与回撤熔断；
  公司行为的价值守恒持仓调整
- **因子研究**：Factor/FactorData 基座、MAD/分位数去极值、zscore、行业市值中性化（OLS 残差）、
  RankIC/ICIR/t 值、分位数分层回测、多空价差、因子自相关
- **报告与实验追踪**：Markdown + JSON 双产物、SQLite 实验库（list/get/compare）、
  回测与因子分析自动留痕并绑定数据快照
- **Agent 接口层**：MCP Server（mcp 1.x/2.x 兼容，11 个工具、统一 JSON 信封、错误必带 hint）、
  策略 AST 静态校验（前视偏差/幻觉 API）、SKILL.md ×3、llms.txt
- **工程化**：uv + ruff + mypy + pytest、GitHub Actions（CI 3.10–3.12 矩阵 + 发布工作流）、
  CLI（`srq init/data/backtest/factor/experiment/mcp`）、示例策略与因子
