# SolidRockQuant

> **The agent-native quant research & backtest framework** — let AI agents work like researchers, let humans review like editors.
>
> [简体中文](README.md) | English

[![CI](https://github.com/samchuit/SolidRockQuant/actions/workflows/ci.yml/badge.svg)](https://github.com/samchuit/SolidRockQuant/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-261230.svg)](https://github.com/astral-sh/ruff)

## Why

Excellent frameworks like vnpy, qlib, backtrader and rqalpha are built "for humans" — AI agents hit walls everywhere:

- Error messages a human can decipher, but an agent just retries blindly;
- Backtest results buried in logs, unreadable and incomparable for an agent;
- No data versioning: the same strategy yields different results across runs, and the agent cannot tell strategy drift from data drift;
- No unified tool protocol, so every agent platform needs bespoke glue code.

SolidRockQuant is designed for LLM agents from day one: **the MCP server is a first-class interface, experiments are tracked automatically, data is versioned, and errors carry actionable hints**. The goal: an agent completes the full research loop — fetch data → write strategies/factors → backtest/analyze → read results → iterate — with minimal human intervention, while humans set goals and review conclusions.

Markets: China A-shares & futures.

## Key Features

### 🤖 Agent-native
- **MCP server** (`srq mcp serve`): 16 tools covering data, backtests, factor analysis and experiment tracking — plug-and-play with Claude and any MCP-capable client
- **Structured output**: unified JSON envelope + Markdown reports; stable error codes with actionable `hint` fields
- **Lookahead detection**: AST-level static checks (`shift(-n)`, `bfill`, hallucinated APIs) run automatically before every backtest
- **Deterministic & reproducible**: no randomness in the engine + immutable data snapshots

### 📊 Data layer
- Pluggable sources: AKShare (EastMoney primary + Sina fallback), Tushare Pro, Baostock; commercial feeds (Wind etc.) via the plugin API; daily + minute bars (1m/5m)
- Unified schema: raw prices + backward adjustment factors (forward-adjusted prices drift on every dividend — never persisted), trading calendar, stock/futures-contract/main-continuous/index/ETF universes
- Local store: atomic Parquet writes + DuckDB parameterized reads, idempotent incremental updates

### ⚙️ Backtest engine (self-built, lightweight)
- Event-driven bar-level engine with **lookahead-safe `next_open` execution by default** (signals at close, filled at next open)
- A-share rules built in: T+1, board-aware price limits (main 10% / ChiNext+STAR 20% / BSE 30%, ST overridable), 100-share lots, suspensions
- Cost model: commission (min ¥5), stamp duty (sell-side), transfer fee, slippage — all configurable; rejections carry reason codes (`LIMIT_UP` / `T_PLUS_ONE` / `WEIGHT_CAP` / `SUSPENDED`…)
- Risk: per-symbol weight cap, drawdown circuit breaker with automatic liquidation
- Futures: contract specs (multiplier/margin/open-close/close-today fees/limits for 17 products), signed dual-direction positions, margin constraints, expiry force-close, main-continuous roll adjustment

### 🔬 Factor research
- `Factor` base class + panel container; MAD/quantile winsorization, z-score, industry/size neutralization (OLS residuals)
- **RankIC / ICIR / t-stat**, quantile layered backtests, long-short spread, factor autocorrelation (turnover proxy)
- Vectorized fast screening channel: factor → long-short weights → NAV in seconds

### 🧪 Experiment tracking (the agent's memory)
- Every backtest/factor run auto-logs: params, config, data snapshot, metrics, artifact paths (SQLite, zero deployment)
- Agents query and compare: "did last week's momentum strategy beat this one?"

## Quick Start

```bash
pip install "solidrock-quant[mcp,sources]"

srq init                                      # initialize data directory
srq data calendar --update                    # fetch trading calendar
srq data update --symbols 510300.SH --start 2024-01-01
```

### Python API

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

### CLI

```bash
srq backtest run examples/dual_ma.py --start 2025-01-01 --end 2026-09-08 --param fast=5
srq factor analyze examples/momentum_factor.py -u 000001.SZ,600519.SH --start 2025-01-01 --end 2026-09-08
srq factor screen examples/momentum_factor.py -u ... --top 0.2 --bottom 0.2   # vectorized screening
srq paper run examples/dual_ma.py --name my-paper        # daily paper trading (persisted state)
srq experiment list                                       # every run is auto-logged
srq data snapshot create snap-20260908                    # data version snapshot
```

### MCP Server (agent integration)

```json
{
  "mcpServers": {
    "solidrock": { "command": "srq", "args": ["mcp", "serve"] }
  }
}
```

16 tools covering the full research loop. See [docs/mcp-setup.md](docs/mcp-setup.md); agent playbooks live in `src/solidrock/agent/skills/`.

## Documentation

| Doc | Content |
|-----|---------|
| [docs/design.md](docs/design.md) | Architecture, data contracts, engine & MCP design, key trade-offs (Chinese) |
| [docs/roadmap.md](docs/roadmap.md) | Roadmap & task breakdown (Chinese) |
| [docs/mcp-setup.md](docs/mcp-setup.md) | Claude Code / Claude Desktop integration (Chinese) |
| [docs/plugins.md](docs/plugins.md) | Writing source & factor plugins (Chinese) |
| [docs/release.md](docs/release.md) | Release process (Chinese) |
| [llms.txt](llms.txt) | LLM-oriented project overview |

## Roadmap

| Version | Content | Status |
|---------|---------|--------|
| v0.1 | Data layer (daily) + event backtest (stocks) + reports + experiment tracking + MCP server | **Done** |
| v0.2 | Factor analysis ✅ · futures backtest ✅ · minute bars ✅ · Baostock ✅ · data health check ✅ · vectorized screening ✅ | **Done** |
| v0.3 | Paper trading ✅ · strategy sandbox ✅ · agent research-loop playbook ✅ · futures CTP live (needs broker environment) | In progress |
| v0.4 | Plugin mechanism ✅ · English docs ✅ · more data sources | In progress |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). CI runs lint (ruff), types (mypy) and tests on Python 3.10–3.12; all four gates must pass. Agent-facing changes should also update `src/solidrock/agent/skills/` and the tool docstrings (they are the LLM-facing documentation).

## Disclaimer

This project is for quantitative research and technical learning only. Backtest results are based on historical data and simplified assumptions and **do not constitute investment advice**. Futures and automated trading involve substantial financial risk; comply with the laws of your jurisdiction and trade at your own risk. Data comes from third-party public APIs with no accuracy guarantee.

## License

[Apache-2.0](LICENSE)
