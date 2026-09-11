# SolidRockQuant

[简体中文](README.md) | English

> **The agent-native quant research & backtest framework** — let AI agents work like researchers, let humans review like editors.
>
> Built for China A-shares (stocks / ETFs / indices / convertible bonds) and domestic futures; overseas equities via the official `yfinance` plugin.

[![CI](https://github.com/samchuit/SolidRockQuant/actions/workflows/ci.yml/badge.svg)](https://github.com/samchuit/SolidRockQuant/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-261230.svg)](https://github.com/astral-sh/ruff)
[![MCP](https://img.shields.io/badge/MCP-23%20tools-6f42c1)](docs/mcp-setup.md)

---

## Why

Excellent frameworks like vnpy, qlib, backtrader and rqalpha are built "for humans" — AI agents hit walls everywhere:

- Error messages a human can decipher, but an agent just retries blindly;
- Backtest results buried in logs, unreadable and incomparable for an agent;
- No data versioning: the same strategy yields different results across runs, and the agent cannot tell strategy drift from data drift;
- No unified tool protocol, so every agent platform needs bespoke glue code.

**SolidRockQuant is designed for LLM agents from day one**: the MCP server is a first-class interface, experiments are tracked automatically, data is versioned, and every error carries an actionable hint. The goal is for an agent to complete the full research loop — fetch data → write strategies/factors → backtest/analyze → read results → iterate — with minimal human intervention, while humans set goals and review conclusions.

---

## Key Features

### 🤖 Agent-native

- **MCP server** (`srq mcp serve`): **23 tools** spanning data, backtesting, factors, ML, experiments, paper trading and live trading — plug-and-play with Claude and any MCP-capable client;
- **Structured output**: a unified JSON envelope (`status` / `data` / `artifacts` / `error` / `next_suggested_tools`) plus Markdown reports; stable error codes with **mandatory actionable `hint` fields**;
- **Lookahead detection**: AST-level static analysis runs before every backtest, catching `shift(-n)`, `bfill` and hallucinated APIs — the most common LLM failure modes;
- **Deterministic & reproducible**: a randomness-free engine plus immutable data snapshots mean the same config always yields the same result;
- **Agent playbooks**: `src/solidrock/agent/skills/` ships backtest, data-update and experiment-comparison workflows.

### 📊 Data layer

- **Pluggable sources**: TDX (direct `pytdx`, white-box `xdxr` adjustment), AKShare (EastMoney primary + automatic Sina fallback), Tushare Pro, Baostock; overseas via the official `yfinance` plugin; commercial feeds (Wind, etc.) extendable through the same API;
- **Frequencies & instruments**: daily and minute bars (1m/5m); stocks, ETFs/LOFs, indices, convertible bonds, futures contracts and main-continuous series;
- **Unified schema**: raw prices + backward-adjustment factors (forward-adjusted prices drift on every dividend and destroy reproducibility, so they are never persisted), trading calendar, normalized symbols (`000001.SZ` / `RB2505.SHFE`);
- **Local store**: atomic Parquet writes + DuckDB parameterized reads, idempotent incremental updates;
- **Data health checks** (`srq data doctor`): gaps, outliers, missing factors, abnormal jumps.

### ⚙️ Backtest engine (self-built, lightweight)

- **Event-driven, bar-level**, with **lookahead-safe `next_open` execution by default** (signals at close, fills at next open);
- **A-share rules built in**: T+1 (with automatic T+0 instrument detection), board-aware price limits (main board 10% / ChiNext & STAR 20% / BSE 30%, ST overridable), 100-share lot validation, suspensions;
- **Cost model**: commission (¥5 minimum) / stamp duty (sell-side) / transfer fee / slippage — all configurable; rejections carry reason codes (`LIMIT_UP` / `T_PLUS_ONE` / `WEIGHT_CAP` / `SUSPENDED` / `NO_MORE_BARS`…);
- **Futures support**: contract specs for 17 products (multiplier / margin rate / open-close and close-today fees / price limits), signed dual-direction positions, margin constraints, expiry force-close, main-continuous roll adjustment;
- **Strategy hooks**: `setup` → `on_market_open` → `on_signal` → `on_market_close` → `on_stop`; minute-frequency `trigger_times` for scheduled intraday firing;
- **Risk controls**: per-symbol weight cap (accounting for existing holdings) and a drawdown circuit breaker with automatic liquidation.

### 🔬 Factor research

- `Factor` base class + panel container (wide tables); MAD / quantile winsorization, z-score, industry & size neutralization (OLS residuals);
- **RankIC / ICIR / t-stat**, quantile layered backtests, long-short spread, factor autocorrelation (turnover proxy);
- **Built-in factors** registered on import: Mom / Reversal / Volatility / Illiq (Amihud) / VWAPDev / AtrRatio;
- **Vectorized screening channel** (`srq factor screen`): factor → long-short weights → NAV in seconds across hundreds of symbols, with a built-in `lag=1` that rules out lookahead.

### 🧮 Indicators / ML / Portfolio

- **Vectorized technical indicators** (`solidrock.indicators`): sma / ema / ref / diff / hhv / llv / cross / roc / bias / macd / rsi / boll / atr / kdj — aligned with TDX/MyTT semantics, working on both Series and multi-symbol wide tables;
- **ML pipeline** (`srq ml walk-forward`): dataset construction → rolling training → strictly out-of-sample prediction, with LightGBM / scikit-learn;
- **Portfolio optimization** (`solidrock.portfolio`).

### 📈 Reporting & performance

- Metrics: cumulative/annualized return, annualized volatility, Sharpe, Sortino, Calmar, max drawdown, alpha/beta, daily win rate, trade win rate, profit factor, max consecutive win/loss days, best/worst single trade, trade count, total fees, annualized turnover, benchmark comparison, monthly returns;
- Artifacts: **Markdown + JSON + self-contained interactive HTML** (plotly: NAV vs benchmark, drawdown, monthly heatmap, return distribution, rolling Sharpe).

### 🧪 Experiment tracking (the agent's memory)

- Every backtest and factor run auto-logs params, config, data snapshot, metrics and artifact paths to SQLite — zero deployment;
- `srq experiment list / show / compare` answers "did last week's momentum strategy beat this one?".

### 🖥️ Paper & live trading

- **Paper trading** (`srq paper run`): persisted daily tracking that reuses the same strategy code and engine rules; state carried across runs includes positions, pending orders, adjustment cursors and circuit-breaker status;
- **Live trading** (`srq live ...`): QMT / cfquant bridge, strategy-driven sessions (open/signal/close phases), position reconciliation;
- **Live safety gates** (see below): read-only by default, order guards, explicit confirmation, full audit trail.

---

## Live Trading Safety (important)

Live trading is the only module that moves real money, so its constraints are **enforced in code — callers cannot bypass them by passing arguments**. `live_submit_order` must pass **all** of the following checks; failing any one rejects the order before it reaches the broker:

| Gate | Setting | Default | Rejection code |
|------|---------|---------|----------------|
| Explicit confirmation | MCP tool argument `confirm=true` | Must be passed explicitly | `PARAM_INVALID` |
| Read-only mode | `SOLIDROCK_LIVE_READ_ONLY` | **`true` (orders rejected)** | `LIVE_READ_ONLY` |
| Symbol allow-list | `SOLIDROCK_LIVE_SYMBOL_WHITELIST` | empty (unrestricted; **recommended**) | `LIVE_SYMBOL_NOT_ALLOWED` |
| Per-order notional cap | `SOLIDROCK_LIVE_MAX_ORDER_NOTIONAL` | ¥500,000 | `LIVE_ORDER_TOO_LARGE` |
| Trading hours | `SOLIDROCK_LIVE_ENFORCE_TRADING_HOURS` | `true` (09:15–11:30 / 13:00–15:05) | `LIVE_NOT_TRADING_HOURS` |
| Idempotency | `SOLIDROCK_LIVE_DUPLICATE_WINDOW_SECONDS` | 60 seconds | `LIVE_DUPLICATE_ORDER` |

Additional design notes:

- **Local validation precedes connection**: read-only, argument and guard checks all run before connecting to QMT, so rejected orders **never touch the broker** and do not require the bridge to be online;
- **Cancels are never time-restricted**: cancelling reduces risk and is allowed at any hour (still subject to read-only);
- **Correct reconciliation semantics**: comparison uses total holdings (`volume`) while sell feasibility uses sellable quantity (`can_use_volume`), preventing the T+1 case where an existing position is misread as a shortfall and re-bought; live positions absent from the target are **not** auto-liquidated by default;
- **Audit and alerting**: submissions, cancellations, blocked attempts and reconciliation diffs are written to `{data_dir}/live/audit.jsonl` and pushed as notifications; notification delivery failures are recorded in `{data_dir}/live/notify_errors.log` so alerting never fails silently.

> ⚠️ Live trading carries substantial financial risk. Validate strategies in paper trading first, and start small with an allow-list and a low notional cap.

---

## Installation

```bash
# From source (will be `pip install solidrock-quant` once published to PyPI)
pip install "solidrock-quant[mcp,sources] @ git+https://github.com/samchuit/SolidRockQuant.git"
```

Optional extras:

| Extra | Contents |
|-------|----------|
| `sources` | AKShare / Tushare / pytdx data-source dependencies |
| `mcp` | MCP server |
| `report` | plotly (interactive HTML reports) |
| `ml` | LightGBM / scikit-learn |
| `dev` | pytest / ruff / mypy / mkdocs |

---

## Quick Start

```bash
srq init                                                    # initialize the data directory
srq data calendar --update                                  # fetch the trading calendar
srq data update --symbols 510300.SH --start 2024-01-01      # fetch bars
```

### Python API

```python
from solidrock import BacktestConfig, BacktestEngine, Context, DataStore, Strategy


class DualMA(Strategy):
    """Dual moving average: go long when the fast MA crosses above the slow MA."""

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
# Backtest (static validation + experiment logging + artifacts, all automatic)
srq backtest run examples/dual_ma.py --start 2025-01-01 --end 2026-09-08 --param fast=5

# Factor research
srq factor analyze examples/momentum_factor.py -u 000001.SZ,600519.SH --start 2025-01-01 --end 2026-09-08
srq factor screen  examples/momentum_factor.py -u ... --top 0.2 --bottom 0.2   # vectorized screening

# ML factor synthesis (rolling training, strictly out-of-sample)
srq ml walk-forward --universe ... --start 2019-01-01 --end 2026-09-08

# Paper trading (persisted state; run once daily after the close)
srq paper run examples/dual_ma.py --name my-paper
srq paper status --name my-paper

# Experiments and data versioning
srq experiment list
srq experiment compare <id1> <id2>
srq data snapshot create snap-20260908

# Diagnostics
srq data doctor
srq data sources
```

### MCP Server (agent integration)

```json
{
  "mcpServers": {
    "solidrock": { "command": "srq", "args": ["mcp", "serve"] }
  }
}
```

The agent then gets **23 tools** (data / backtest / factors / ML / experiments / paper / live).
The full tool list and live-trading safety configuration are in [docs/mcp-setup.md](docs/mcp-setup.md) (Chinese).

---

## Data Conventions (single source of truth)

- **Storage**: raw prices + backward-adjustment factors (`hfq = close × adj_factor`). **Forward-adjusted prices are never persisted** — they drift on every dividend, which would destroy historical reproducibility;
- **Units**: `volume` is in **lots**, `amount` in **yuan**; `pre_close` is the post-adjustment previous close;
- **Daily columns**: `symbol, date, open, high, low, close, pre_close, volume, amount, turnover_rate, adj_factor, suspended`; futures add `settle, open_interest`;
- **Symbols**: `000001.SZ` / `600519.SH` / `RB2505.SHFE` / main-continuous `RB.SHFE`.

---

## Project Layout

```
src/solidrock/
├── data/          # Data layer: pluggable sources, local store (Parquet+DuckDB), calendar, symbols, health checks
├── backtest/      # Event-driven engine, matching (A-share/futures rules), portfolio accounting, costs, paper trading, vectorized backtest
├── strategy/      # Strategy base class and loader
├── factors/       # Factor base, cross-sectional processing, RankIC/layered analysis, built-ins, reports
├── indicators.py  # Vectorized technical indicator library
├── ml/            # ML pipeline: datasets, walk-forward, model wrappers
├── portfolio/     # Portfolio optimization
├── report/        # Performance metrics + Markdown / JSON / HTML reports
├── experiments/   # SQLite experiment tracking
├── risk/          # Weight caps, drawdown circuit breaker
├── live/          # Live: QMT/cfquant bridge, order guards, reconciliation, strategy-driven sessions
├── agent/         # MCP server, error spec, strategy static validation, SKILL.md
├── cli/           # `srq` command line
└── utils/         # Shared utilities
```

---

## Quality Gates

| Check | Status |
|-------|--------|
| Unit tests | **389 passing** (`pytest -m "not network"`) |
| Coverage | **79%** (core backtest-engine modules > 90%) |
| Type checking | `mypy src/solidrock` — no issues |
| Lint / format | `ruff check` + `ruff format --check` — clean |
| CI | lint + test matrix on Python 3.10/3.11/3.12 + CLI smoke + docs build |

Before submitting:

```bash
ruff check src tests && ruff format --check src tests
mypy src/solidrock
pytest -m "not network"
```

---

## Documentation

| Doc | Content |
|-----|---------|
| [docs/design.md](docs/design.md) | Architecture, data contracts, engine & MCP design, key trade-offs (Chinese) |
| [docs/mcp-setup.md](docs/mcp-setup.md) | MCP setup, tool list, **live-trading safety gates** (Chinese) |
| [docs/agent-workflow.md](docs/agent-workflow.md) | Agent research-loop playbook (Chinese) |
| [docs/tutorial-agent-research.md](docs/tutorial-agent-research.md) | Tutorial: factor research with Claude (Chinese) |
| [docs/plugins.md](docs/plugins.md) | Writing data-source and factor plugins (Chinese) |
| [docs/roadmap.md](docs/roadmap.md) | Roadmap and task breakdown (Chinese) |
| [docs/release.md](docs/release.md) | Release process (Chinese) |
| [Documentation site](https://samchuit.github.io/SolidRockQuant/) | Online version of the above |
| [CHANGELOG.md](CHANGELOG.md) | Change log |
| [llms.txt](llms.txt) | LLM-oriented project overview |

---

## Roadmap

| Version | Content | Status |
|---------|---------|--------|
| v0.1 | Data layer (daily) + event backtest (stocks) + reports + experiment tracking + MCP server | **Done** |
| v0.2 | Factor analysis · futures backtest (margin / dual-direction / roll) · minute bars · data health checks | **Done** |
| v0.3 | Paper trading · strategy sandbox · agent research-loop example | **Done** |
| v0.4 | Plugin registry (`plugins/` workspace) · English docs | **Done** |
| v0.5 | Minute-frequency backtests · overseas symbols · official yfinance plugin | **Done** |
| v0.6 | ML pipeline · portfolio optimization · notifications · equity live trading (QMT / cfquant bridge) | **Done** |
| v0.7 | Indicator library · built-in factors · HTML reports · pre/post-market hooks · T+0 matching · TDX source · live order guards | **In progress** |
| v0.8 | Futures CTP live trading · more data sources · multi-turn agent research orchestration | Planned |

See [CHANGELOG.md](CHANGELOG.md) for detailed per-release changes.

---

## Contributing

The project is in an early, fast-moving stage. Issues and PRs discussing design are welcome:

1. Fork → create a feature branch → run `ruff check` / `mypy src/solidrock` / `pytest -m "not network"` before submitting;
2. New features should ship with tests (CI runs the full suite on Python 3.10–3.12);
3. **Agent-facing changes must also update `src/solidrock/agent/skills/` and the tool docstrings** — those are the LLM-facing documentation;
4. Changes touching live trading must add tests covering every new allow path against the safety gates.

See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

---

## Disclaimer

This project is for quantitative research and technical learning only. Backtest results are based on historical data and simplified assumptions and **do not constitute investment advice**. Futures and automated trading involve substantial financial risk; comply with the laws of your jurisdiction and trade at your own risk. Data comes from third-party public APIs with no accuracy guarantee.

## License

[Apache-2.0](LICENSE)
