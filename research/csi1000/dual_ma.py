"""双均线择时（可带防守资产）.

逻辑：快均线 > 慢均线 → 满仓风险资产；否则切防守资产（空串=空仓）。
每日重发目标（引擎 0.5% 死区抑制无效单，被拒腿次日自愈）；
卖单先于买单提交。
"""

from solidrock import Context, Strategy


class DualMASym(Strategy):
    params = {
        "symbol": "512100.SH",
        "fast": 10,
        "slow": 60,
        "defensive": "511010.SH",
    }

    def setup(self, ctx: Context) -> None:
        self._risk = str(self.params["symbol"])
        self._def = str(self.params["defensive"])
        ctx.universe = [s for s in (self._risk, self._def) if s]

    def on_signal(self, ctx: Context) -> None:
        fast, slow = int(self.params["fast"]), int(self.params["slow"])
        close = ctx.history(self._risk, slow + 1, fields="close")[self._risk]
        fac = ctx.history(self._risk, slow + 1, fields="adj_factor")[self._risk].fillna(1.0)
        adj = close * fac
        window = adj.iloc[-slow:]
        if window.isna().any() or len(window) < slow:
            return
        above = bool(adj.iloc[-fast:].mean() > window.mean())
        legs = [(self._risk, 1.0 if above else 0.0)]
        if self._def:
            legs.append((self._def, 0.0 if above else 1.0))
        for sym, w in sorted(legs, key=lambda t: t[1]):
            ctx.order_target_percent(sym, w)
