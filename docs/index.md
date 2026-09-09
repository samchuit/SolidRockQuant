# SolidRockQuant · 磐石智擎

**Agent 原生的量化研究与回测框架**（A股/期货）——让 AI Agent 像研究员一样工作，让人类像审稿人一样把关。

[English README](https://github.com/samchuit/SolidRockQuant/blob/main/README.en.md) · [GitHub 仓库](https://github.com/samchuit/SolidRockQuant)

## 安装

```bash
pip install "solidrock-quant[mcp,sources]"
```

## 快速上手

```python
from solidrock import BacktestConfig, BacktestEngine, Context, DataStore, Strategy

class DualMA(Strategy):
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

## 让 Agent 接管研究闭环

配置 [MCP Server](mcp-setup.md) 后，Claude 等 Agent 可用 16 个工具完成
"筛选 → 显著性确认 → 回测 → 对比 → 模拟盘"全流程，参见
[Agent 研究闭环](agent-workflow.md)。

## 文档导航

- [技术设计](design.md) —— 架构、数据规范、引擎与 MCP 设计
- [路线图](roadmap.md) —— 版本里程碑
- [MCP 接入](mcp-setup.md) —— Claude Code / Desktop 配置
- [插件开发](plugins.md) —— 第三方数据源/因子接入
- [发布流程](release.md) —— Trusted Publishing 双站发布

## 免责声明

仅供量化研究与技术学习，不构成投资建议。回测基于历史数据与简化假设；
期货与自动交易风险重大，请遵守所在地法律法规并自担风险。
