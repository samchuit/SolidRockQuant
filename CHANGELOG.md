# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.1.0.dev0] - 2026-09-08

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
