"""买入持有策略示例（最简策略，用于验证引擎与数据链路）.

运行回测::

    srq backtest run examples/buy_and_hold.py --start 2024-01-01 --end 2025-12-31
"""

from solidrock import Context, Strategy


class BuyAndHold(Strategy):
    params = {"symbol": "510300.SH", "percent": 1.0}

    def setup(self, ctx: Context) -> None:
        ctx.universe = [str(self.params["symbol"])]

    def on_signal(self, ctx: Context) -> None:
        symbol = str(self.params["symbol"])
        if ctx.position(symbol) == 0:
            ctx.order_target_percent(symbol, float(self.params["percent"]))
