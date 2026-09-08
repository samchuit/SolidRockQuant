"""策略基类.

用户只需继承 :class:`Strategy` 并实现 ``setup`` / ``on_signal``::

    class DualMA(Strategy):
        params = {"fast": 5, "slow": 20}

        def setup(self, ctx: Context) -> None:
            ctx.universe = ["510300.SH"]

        def on_signal(self, ctx: Context) -> None:
            close = ctx.history("510300.SH", self.params["slow"] + 1, fields="close")["510300.SH"]
            if len(close) < self.params["slow"]:
                return
            if close.iloc[-5:].mean() > close.iloc[-20:].mean():
                ctx.order_target_percent("510300.SH", 1.0)
            else:
                ctx.order_target_percent("510300.SH", 0.0)

生命周期（v0.1 日频）：
- ``setup``：数据加载前调用，设置 ``ctx.universe``；
- ``on_signal``：每个交易日收盘后调用一次（bar 数据已包含当日），
  产生的订单按引擎执行模式在次日开盘（默认）或当日收盘成交。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from solidrock.backtest.context import Context


class Strategy:
    """策略基类。子类通过 ``params`` 声明参数（实例化时可覆盖）。"""

    # 公共 API：类级参数声明；实例化后 self.params 为合并了覆盖值的实例属性
    params: dict[str, Any] = {}

    def __init__(self, **param_overrides: Any) -> None:
        unknown = set(param_overrides) - set(type(self).params)
        if unknown:
            from solidrock.agent.errors import ErrorCode, err

            raise err(
                ErrorCode.PARAM_INVALID,
                f"策略 {type(self).__name__} 未声明参数：{sorted(unknown)}",
                hint=f"可用参数：{sorted(type(self).params)}；如需新增请加到类属性 params",
            )
        self.params = {**type(self).params, **param_overrides}

    def setup(self, ctx: Context) -> None:
        """可选：设置 ctx.universe 等；在数据加载前调用。"""

    def on_signal(self, ctx: Context) -> None:
        """核心钩子：每个交易日调用一次，通过 ctx 下单。"""

    def on_stop(self, ctx: Context) -> None:
        """可选：回测结束（最后一个 bar 之后）调用。"""
