"""单均线择时（可带防守资产）.

逻辑：风险资产复权收盘价 > MA(ma) → 满仓风险资产；否则切防守资产
（defensive 为空串时纯空仓）。信号收盘生成、次日开盘成交（引擎默认 next_open）。

实现要点：
- 每日重发目标仓位：引擎的 0.5% 死区会抑制无变化下单；若某条腿被拒
  （如涨停），次日自动补单自愈；
- 卖单先于买单提交：引擎按队列顺序实时校验现金，买单在前会因现金不足被拒。
"""

from solidrock import Context, Strategy


class MaTiming(Strategy):
    params = {
        "symbol": "512100.SH",     # 风险资产
        "ma": 60,                  # 均线窗口
        "defensive": "511010.SH",  # 防守资产（国债ETF），空串=空仓
    }

    def setup(self, ctx: Context) -> None:
        self._risk = str(self.params["symbol"])
        self._def = str(self.params["defensive"])
        ctx.universe = [s for s in (self._risk, self._def) if s]

    def on_signal(self, ctx: Context) -> None:
        ma = int(self.params["ma"])
        close = ctx.history(self._risk, ma + 1, fields="close")[self._risk]
        fac = ctx.history(self._risk, ma + 1, fields="adj_factor")[self._risk].fillna(1.0)
        adj = close * fac  # 复权价信号（份额合并/分红不影响均线）
        window = adj.iloc[-ma:]
        if window.isna().any() or len(window) < ma:
            return  # 指标未就绪（回测初期），维持现状
        above = bool(adj.iloc[-1] > window.mean())
        legs = [(self._risk, 1.0 if above else 0.0)]
        if self._def:
            legs.append((self._def, 0.0 if above else 1.0))
        for sym, w in sorted(legs, key=lambda t: t[1]):
            ctx.order_target_percent(sym, w)
