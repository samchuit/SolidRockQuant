"""期货空头示例策略（螺纹钢合约）.

数据准备::

    srq data update --symbols RB2505.SHFE --start 2024-09-01

运行回测（引擎自动按合约规格处理乘数/保证金/平今费率，到期自动强平）::

    srq backtest run examples/futures_short.py --start 2024-10-08 --end 2025-04-30 --param lots=10

逻辑：开空后，价格较开仓均价下跌 ``take_profit``（默认 3%）即买入回补止盈；
回测结束时仍未触发则持有至最后（合约到期前引擎会强制平仓）。
"""

from solidrock import Context, Strategy


class FuturesShort(Strategy):
    params = {"symbol": "RB2505.SHFE", "lots": 5, "take_profit": 0.03}

    def setup(self, ctx: Context) -> None:
        symbol = str(self.params["symbol"])
        ctx.universe = [symbol]

    def on_signal(self, ctx: Context) -> None:
        import pandas as pd

        symbol = str(self.params["symbol"])
        lots = int(self.params["lots"])
        tp = float(self.params["take_profit"])
        pos = ctx.position(symbol)
        if pos == 0:
            ctx.order(symbol, -lots)  # 开空
            ctx.log(f"开空 {lots} 手")
            return
        if pos < 0:  # 持有空头，检查止盈
            entry = ctx.portfolio.position(symbol).avg_cost
            price = ctx.history(symbol, 1, fields="close")[symbol].iloc[-1]
            if not pd.isna(price) and price <= entry * (1 - tp):
                ctx.order(symbol, lots)  # 买入回补
                ctx.log(f"止盈平空：entry={entry:.0f} price={price:.0f}")
