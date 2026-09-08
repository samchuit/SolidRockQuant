# SolidRockQuant 路线图

> 原则：每个版本都保持"可安装、可运行、可演示"，MVP 就带 MCP Server（传播点先行）。

## v0.1 —— 研究回测 MVP（当前版本）

目标：一个 Agent 能通过 MCP 独立完成"拉数据 → 回测 → 读报告 → 对比实验"的闭环，人类用 CLI 也能完成同样的事。

### 任务拆解

**M1 工程基建 ✅**
- [x] 仓库初始化：pyproject（uv）、ruff/mypy/pytest 配置、GitHub Actions（lint+test）、Apache-2.0 LICENSE
- [x] 全局配置：pydantic-settings（数据目录、token、费率默认值）
- [x] 错误体系：错误码枚举 + hint 规范（`agent/errors.py`，全库统一抛 `SolidRockError` 子类）

**M2 数据层（日线，股票/指数/ETF）✅**
- [x] 标准 schema + 符号约定校验（`000001.SZ` 格式）
- [x] `DataSource` 基类 + 注册表
- [x] AKShare 适配器：日线行情、交易日历、股票列表、指数成分（东财主通道 + 新浪自动回退，按 akshare 1.18.94 实测结构适配）
- [x] Tushare Pro 适配器：同上（需 token）
- [x] 本地存储：Parquet 落盘（原子写入）+ DuckDB 参数化读取 + 增量更新（幂等 + NaN coalesce）
- [x] 数据快照：`snapshot(tag)` / 按 tag 只读读取（硬链接近零拷贝）

**M3 回测引擎（股票）✅**
- [x] 事件主循环 + `Clock` 抽象（bar 级）
- [x] `Strategy` 基类、`Context` API（order / history / portfolio / schedule）
- [x] 撮合：next_open 默认、涨跌停限制、T+1、停牌处理、same_close 可选
- [x] 费用模型：佣金/印花税/过户费/滑点，可配置
- [x] 组合核算与净值曲线（含复权因子变化的价值守恒持仓调整）
- [x] 简版风控：仓位上限、回撤熔断钩子

**M4 报告与实验追踪 ✅**
- [x] metrics：年化/夏普/最大回撤/卡玛/胜率/换手率/基准对比（默认基准 000300.SH）
- [x] 产物：report.md、result.json、trades.csv、nav.csv
- [x] SQLite 实验追踪：自动入库、list/get/compare API

**M5 Agent 接口层 ✅**
- [x] MCP Server（mcp 2.x MCPServer / 1.x FastMCP 兼容）+ 工具：get_data_overview / list_data_sources / search_instruments / fetch_bars / get_trading_calendar / validate_strategy / run_backtest / list_experiments / get_experiment / compare_experiments
- [x] validate_strategy：AST 静态检查（语法、前视偏差 shift(-n)/bfill、Context API 误用）；run_backtest 前自动执行
- [x] SKILL.md ×3：回测流程、数据更新流程、实验对比流程（随包分发）
- [x] Claude Code / Claude Desktop 接入配置文档（docs/mcp-setup.md）+ llms.txt + 统一 JSON 信封（错误必带 hint）

**M6 交付**
- [x] CLI：`srq init / data update / data calendar / data peek / data stats / data snapshot / data sources / backtest run / experiment list|show|compare`
- [x] 示例策略（dual_ma、buy_and_hold）；CI 冒烟测试随 M5 补充
- [ ] mkdocs 文档站骨架 + 快速开始
- [ ] README 打磨（中文先行，英文 v0.4）、发布 PyPI 测试源验证安装

### 验收标准（v0.1 Definition of Done）

1. ✅ `pip install -e .` 后，示例策略三条命令内跑出完整报告
2. ✅ Claude Code 挂载 MCP Server 后，仅凭 SKILL.md 指引能独立完成一次回测并读回结果（已用 MCP stdio 协议端到端验证：validate_strategy → run_backtest → get_experiment）
3. ✅ 同一策略 + 同一数据快照，两次运行指标完全一致（可复现性测试进 CI）
4. ✅ 故意写一个前视偏差策略，`validate_strategy` 能报出 `LOOKAHEAD_SUSPECTED`

## v0.2 —— 期货与因子（进行中）

- [x] **因子模块完整版**：FactorData 面板 + Factor 基类、MAD/分位数去极值、zscore、排名、行业市值中性化（回归取残差）、RankIC/ICIR/t 值、分位数分层回测、多空价差、因子自相关；CLI `srq factor analyze`、MCP `run_factor_analysis`、实验留痕（kind=factor）、示例 momentum_factor
- [x] **期货回测支持**：合约规格表（乘数/保证金/开平今费率/涨跌停，可覆盖）、带符号双向持仓（开/平/翻转）、平今拆分费率、保证金约束、到期强平（近似到期日）、主连换月比例复权工具（`roll_adjust_continuous`）、示例 futures_short
- [x] **数据体检**：`srq data doctor` + MCP `data_health`——缺失交易日/OHLC 异常/因子缺失/零成交连段/大幅波动
- [ ] 分钟线支持（AKShare/Tushare 分钟数据，存储分区扩展）
- [ ] Baostock 适配器
- [ ] 向量化快速回测通道（大范围因子筛选用）
- [ ] 期货指标语义细化（交易胜率按开/平方向区分）

## v0.3 —— 走向实盘

- 模拟盘（paper trading）：引擎对接实时/延迟行情，每日定时跑
- 期货实盘：CTP / openctp 网关（依赖 vnpy gateway 或独立实现，届时评估）
- 股票自动交易：先做只读对账（持仓核对），下单接口涉及券商合规，单独评估后决定
- 策略沙箱执行（子进程 + 资源限制）
- 多 Agent 工作流示例：策略生成 Agent → 回测验证 Agent → 报告 Agent 的完整 demo

## v0.4 —— 生态

- 插件注册机制（第三方数据源/费用模型/风控规则以 entry-points 接入）
- 英语文档与国际化社区
- 更多数据源（yfinance 海外、商业接口预留位）
- 策略市场：社区策略模板库与回测榜单

## 风险与待决事项

- **券商下单合规**：A股自动化下单处于灰色地带，v0.3 前需要明确只做期货实盘 or 提供接口由用户自担责任
- **免费数据源稳定性**：AKShare 接口偶发变动，适配器需要版本锁定 + 健康检查
- **Tushare 积分门槛**：部分接口需 2000 积分，文档中要写清楚各源的能力边界
- **MCP 生态演进**：MCP 协议仍在快速迭代，工具层保持薄封装以便跟进
