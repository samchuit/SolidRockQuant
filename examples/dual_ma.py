"""双均线策略示例.

数据准备::

    srq data update --symbols 510300.SH --start 2023-01-01

运行回测::

    srq backtest run examples/dual_ma.py --start 2024-01-01 --end 2025-12-31 --param fast=5 --param slow=20

逻辑：快均线（默认 5 日）上穿慢均线（默认 20 日）满仓，反之清仓。
"""

from solidrock import Context, Strategy


class DualMA(Strategy):
    params = {"fast": 5, "slow": 20}

    def setup(self, ctx: Context) -> None:
        ctx.universe = ["510300.SH"]

    def on_signal(self, ctx: Context) -> None:
        fast, slow = int(self.params["fast"]), int(self.params["slow"])
        close = ctx.history("510300.SH", slow + 1, fields="close")["510300.SH"]
        window = close.iloc[-slow:]
        if window.isna().any() or len(window) < slow:
            return  # 数据不足（回测初期或停牌）
        target = 1.0 if window.iloc[-fast:].mean() > window.mean() else 0.0
        ctx.order_target_percent("510300.SH", target)
        ctx.log(f"fast_ma={window.iloc[-fast:].mean():.3f} slow_ma={window.mean():.3f} target={target}")
