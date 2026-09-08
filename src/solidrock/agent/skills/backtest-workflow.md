# SKILL: 回测研究流程

目标：让 Agent 独立完成"检查数据 → 写策略 → 校验 → 回测 → 读结果 → 迭代"的完整闭环。

## 0. 前置认知

- 所有 MCP 工具返回 **JSON 信封**：`status`("ok"/"error") + `data` + `artifacts`(文件路径) + `error.{code,message,hint}`。
- 出错时先读 `error.hint` 并按它行动，不要盲目重试相同参数。
- 符号格式：`000001.SZ` / `600519.SH` / `000300.SH`(指数) / `510300.SH`(ETF) / `RB2505.SHFE`(期货) / `RB.SHFE`(主连)。
- 引擎默认 `next_open` 执行（收盘信号次日开盘成交，防前视）；不要为了"更好的回测收益"改用 `same_close`。

## 1. 检查本地数据

调用 `get_data_overview`，确认：
1. 回测区间内的交易日历已覆盖（否则 `get_trading_calendar` 传 `update=true`）；
2. 目标标的日线已缓存且区间覆盖"回测起点往前 250 个交易日"（策略 warmup）；
   缺数据 → `fetch_bars(symbols=[...], start=<回测起点-365天>, end=今天)`。

## 2. 编写策略文件

在项目工作区写一个 `.py` 文件（内含**一个** Strategy 子类）：

```python
from solidrock import Context, Strategy


class MyStrategy(Strategy):
    params = {"symbol": "510300.SH", "fast": 5, "slow": 20}

    def setup(self, ctx: Context) -> None:
        ctx.universe = [str(self.params["symbol"])]  # 必须设置

    def on_signal(self, ctx: Context) -> None:  # 每个交易日收盘后调用
        symbol = str(self.params["symbol"])
        close = ctx.history(symbol, int(self.params["slow"]) + 1, fields="close")[symbol]
        window = close.iloc[-int(self.params["slow"]) :]
        if window.isna().any() or len(window) < int(self.params["slow"]):
            return  # 数据不足（warmup 期/停牌）直接跳过
        target = 1.0 if window.iloc[-int(self.params["fast"]) :].mean() > window.mean() else 0.0
        ctx.order_target_percent(symbol, target)
```

可用 API（只有这些，幻觉调用会被静态检查拦截）：
- 下单：`ctx.order(sym, qty)`、`ctx.order_value(sym, value)`、`ctx.order_target_percent(sym, pct)`、`ctx.order_target_value(sym, value)`、`ctx.cancel_all()`
- 数据：`ctx.history(symbols, n, fields="close")`（宽表 DataFrame，含当日 bar）、`ctx.position(sym)`
- 查询：`ctx.portfolio`（cash/positions/total_value）、`ctx.now`、`ctx.params`
- 其他：`ctx.log(msg)`

硬性规则：
- **只能用截至当前 bar 的数据**（`shift(-n)`、`bfill` 会被拦截）；
- 参数全部通过 `self.params` 声明与读取，方便对比实验；
- 仓位目标用 `order_target_percent`，不要手写股数对齐逻辑（引擎处理整手/T+1/资金约束）。

## 3. 校验并回测

1. `validate_strategy(strategy_file=<绝对路径>)` — 有 error 必须先修；
2. `run_backtest(strategy_file=<绝对路径>, start="YYYY-MM-DD", end="YYYY-MM-DD", params={...}, name="<可辨识的实验名>")`；
3. 读返回的 `metrics`（核心：annual_return / sharpe / max_drawdown / trade_win_rate / annual_turnover / excess_annual_return）与 `rejection_counts`。

## 4. 迭代

- `list_experiments` → `compare_experiments(run_ids=[...])` 横向对比；
- 每次改动只动一个变量（一个参数或一处逻辑），实验名带上改动点（如 "双均线-fast10"）；
- 警惕过拟合：参数扫描得到的"最优"参数要换区间/换标的验证；
- 交易胜率低但盈亏比高是趋势策略常态，单看胜率会误判。

## 5. 常见错误码

| code | 含义 | 处置 |
|------|------|------|
| NO_DATA | 本地无数据/区间无交易日 | fetch_bars 补数据或修正区间 |
| SYMBOL_INVALID | 符号格式错 | 用 search_instruments 确认 |
| LOOKAHEAD_SUSPECTED | 疑似前视 | 按行号修复，不要绕过 |
| STRATEGY_INVALID | 语法/API 误用 | 按 issues 修复 |
| WEIGHT_CAP | 触发仓位上限 | 正常风控行为；若目标仓位>上限属策略设计问题 |
