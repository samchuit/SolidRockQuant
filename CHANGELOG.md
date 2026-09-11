# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 修复（安全）

- **实盘只读配置曾被硬编码绕过**：`agent/tools.py`（MCP `live_submit_order`/
  `live_cancel_order`）与 `cli/main.py`（`live order`/`live cancel`/`live reconcile`
  /`live session`）在下单时硬编码 `read_only=False`，导致 `SOLIDROCK_LIVE_READ_ONLY=true`
  **对任何真实下单路径都没有约束力**——与 `docs/mcp-setup.md`、`README`、`llms.txt`
  的承诺相反。现改为继承全局配置（`read_only=None` → 读 `SOLIDROCK_LIVE_READ_ONLY`），
  CLI 的 `--read-only/--no-read-only` 死参数已真正生效；`live session --execute`
  在只读时提前报错而非跑空。测试从"只测 broker 类"改为**直接打工具调用层**
  （`tests/test_mcp_tools.py::TestLiveToolSafety`），避免再次出现"配置失效却测试全绿"。
- **新增实盘下单守卫 `live/guards.py`（`LiveOrderGuard`）**：在
  `CfquantBroker.submit_order` 内**无条件**执行，调用方无法通过传参跳过。四道闸门：
  标的白名单（`SOLIDROCK_LIVE_SYMBOL_WHITELIST`）、单笔金额上限
  （`SOLIDROCK_LIVE_MAX_ORDER_NOTIONAL`，默认 50 万）、交易时段
  （`SOLIDROCK_LIVE_ENFORCE_TRADING_HOURS`，默认开，09:15-11:30 / 13:00-15:05）、
  幂等去重（`SOLIDROCK_LIVE_DUPLICATE_WINDOW_SECONDS`，默认 60s，防 LLM 重试重复下单）。
  新错误码：`LIVE_SYMBOL_NOT_ALLOWED` / `LIVE_ORDER_TOO_LARGE` /
  `LIVE_NOT_TRADING_HOURS` / `LIVE_DUPLICATE_ORDER`。
- **MCP `live_submit_order` 新增 `confirm` 参数**：未显式传 `confirm=true` 一律拒绝
  （代码层闸门，此前仅靠提示词约定）。撤单不受时段限制（降低风险的动作）。
- **下单路径本地校验前置**：只读/参数/守卫检查全部先于 `_connect()`，因此被拒的订单
  **不会触碰 QMT**，也不需要桥接在线。
- **对账口径与清仓保护**：`live/reconcile.py` 此前用 `can_use_volume`（可卖量）当
  **实际持仓**对比，A 股 T+1 下会把"已持有"误报为"缺仓"，配合 `--yes`
  会**重复买入**。现改为：对比用 `volume`（总持仓）、卖出受可卖量约束
  （T+1 锁定部分不卖并附 note）、新增 `hold` 动作；新增 `untracked_positions()`，
  目标未包含的实盘持仓**默认不自动清仓**（`liquidate_untracked=False`）。
- **回测单标的权重上限未计入已有持仓**：`risk/checks.py:cap_qty` 把 `current_value`
  硬编码为 `0.0`，导致反复加仓时每笔都能再买满 `max_weight`——**95% 上限可被突破到
  接近 100%**。现接受 `current_value` 由引擎传入（已持仓市值），并补回归测试
  `test_weight_cap_not_bypassed_by_repeated_adds`。

### 修复（数据正确性）

- **TDX 数据源补测试**：`data/sources/tdx_source.py`（379 行，此前**零测试**）新增
  `tests/test_tdx_source.py`（21 个用例），重点覆盖白盒 xdxr **复权因子重建链路**
  （现金分红、送股、扩缩股、多次事件连乘、无操作事件忽略、越界比值拒绝、xdxr 失败降级），
  以及翻页提前收手、指数走 `get_index_bars`、分钟线 category 映射、服务器故障转移。
  覆盖率 15% → 85%。

### 修复（研究报告口径）

- `research/grid_research.py` 用**未复权价**跑网格：512100 在 2022-09-05 份额合并
  （原始价 0.982→2.713）造成的非经济性跳变被当作连续行情，在虚高价卖出导致收益虚增。
  改用 `adjust="hfq"` 后，step8%/U10 由 8.63%/-15.9% 修正为 **3.46%/-19.8%**
  （同窗口买入持有 13.09% → 2.17%）。
- `research/pit_reeval.py` 的"PIT 无偏宇宙"从未生效：`pit_universe()` 只被从未调用的
  `run_pit` 引用，产出 CSV 的 `pit_weights` 实际用的是 2799 只全历史成分并集。
  改为逐期取当期成分后：等权 **+4.22% → +0.66%**、REV20 **+2.98% → +1.42%**
  （即"等权化结构性收益"从宣称的 +13.9pp 收缩到约 +0.7pp）。
- `research/ml_synth.py` 用"当前 1000 只成分"训练/回测（幸存者偏差）：宇宙改为历史成分
  并集（2798 只，含退市股）、逐期 PIT 掩码（平均 1000 只/期）、移除依赖总市值的
  EP/SIZE 特征（市值仅覆盖当前成分）。ML Top-100 由 23.5% 修正为 **2.23%**，
  同口径 PIT 等权基线 5.72% —— **选股超额为负**，原"最高收益"结论推翻。
- 新增 `research/pit_universe.py`（PIT 成分掩码共用工具）；`portfolio_backtest.load_panels`
  与 `fund_factor_research.load_bars_panel/build_factor_panels` 支持传入自定义宇宙。
- 研究报告（`research/PROGRAM_REPORT.md`、`research/csi1000/REPORT.md`、
  `REPORT_stocks.md`）已按上述修正结果更新并标注不可复现的数字。

### 文档

- MCP 工具数统一为 **23**（README 此前一处写 13、一处写 22，实际 23）；
- README 路线图补齐至 v0.8（原表停留在 v0.2"进行中"，实际已到 0.6/0.7）；
- 消除"默认只读"的表述与实现不一致：`docs/mcp-setup.md` 新增"实盘下单的安全闸门"
  对照表（含配置项、默认值、拒绝码），`llms.txt`、`README`、`.env.example` 同步更新；
- `.gitignore` 增加 `research/` 白名单：脚本与结论报告入库（负面结论同样是资产），
  大体积数据产物（parquet/csv/html）继续忽略。

### 新增

- **HTML 交互回测报告**（plotly，extras `report`）：`report/html.py` 渲染单文件自包含
  HTML——净值 vs 基准 + 回撤、月度收益热力图、日收益分布、滚动夏普、核心指标表、
  拒单表；引擎产物链自动写出 `report.html`（plotly 未安装时优雅跳过），
  MCP `run_backtest` 产物列表按存在性返回
- **向量化指标库**：`solidrock/indicators.py`——sma/ema/ref/diff/hhv/llv/cross/
  roc/bias/macd/rsi/boll/atr/kdj，兼容 Series 与多标的宽表，平滑口径对齐通达信/MyTT
- **内置因子**：`factors/builtin.py` 导入即注册——Mom / Reversal / Volatility /
  Illiq（Amihud）/ VWAPDev / AtrRatio；`resolve_factor` 统一"注册名或文件路径"
  解析，`srq factor analyze/screen` 与 MCP 因子工具均支持内置因子名直用
- **策略盘前/盘后钩子**：`Strategy.on_market_open` / `on_market_close`（日频每
  交易日一次；分钟频=当日首/末 bar），统一 bar 级 no-lookahead 语义（订单按执行
  模式撮合）；分钟频 `BacktestConfig.trigger_times` 定时触发（如 `["09:31","14:55"]`），
  CLI `srq backtest run --freq --trigger-times`
- **策略驱动实盘会话**：`live/session.py` `LiveSession` + `srq live session`——
  复用回测策略 API（Context/EngineState）与本地数据，组合快照来自 QMT 实时查询
  （字段防御式映射），意图订单翻译为实盘委托（买入整手、市价/限价、审计日志）；
  默认 dry-run，真实下单需 `--execute` 且关闭只读；`--phase auto` 常驻调度
  （盘前 open → 定时 signal → 盘后 close + 通知），三阶段摘要落盘
  `{data_dir}/live/sessions/`
- **T+0 品种撮合规则**：`Symbol.is_t0` 尽力推断（沪市 511/513/518 段、深市常见
  跨境/黄金/商品 ETF、期货），T0 品种当日买入当日可卖（`Portfolio.buy`
  `immediate_available`），分钟回测下与 T+1 品种行为可验证区分
- **绩效指标补齐**：索提诺、Jensen's alpha / beta、月度收益（`monthly_returns`）、
  最大连续盈利/亏损天数、单笔最大盈亏；Markdown/CLI 报告同步展示

### 修复

- **`scipy` 依赖未声明**：`portfolio/optimizer.py` 引用 scipy 但 pyproject 未声明
  （此前靠 ml extras 中 sklearn 间接带入），独立安装基础包即 `ImportError`——
  现显式声明于核心依赖
- `metrics.compute_metrics` 负净值时年化计算触发 `RuntimeWarning`（现降级为 0）
- tdx 数据源 `_get_xdxr` 中无效的 `self.__class__ and ...` 表达式导致的 mypy
  union-attr 报错；清理无效 `noqa`、未使用变量与超长行（cli/tdx_source）

（v0.7 余项：期货实盘需 CTP 环境）

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
