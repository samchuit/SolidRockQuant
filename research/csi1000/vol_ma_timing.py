"""均线方向 + 波动率目标仓位.

逻辑：复权收盘 > MA(ma) 时，风险仓位 = min(1, target_vol / 已实现年化波动)，
其余给防守资产（空串=空仓）；收盘 <= MA 时风险仓位 0。
波动率用近 vol_win 日复权收益标准差年化。
每日重发目标（0.5% 死区抑制无效单），卖单先于买单提交。
"""

from solidrock import Context, Strategy


class VolMaTiming(Strategy):
    params = {
        "symbol": "512100.SH",
        "ma": 60,
        "vol_win": 20,
        "target_vol": 0.25,
        "defensive": "511010.SH",
    }

    def setup(self, ctx: Context) -> None:
        self._risk = str(self.params["symbol"])
        self._def = str(self.params["defensive"])
        ctx.universe = [s for s in (self._risk, self._def) if s]

    def on_signal(self, ctx: Context) -> None:
        ma = int(self.params["ma"])
        vw = int(self.params["vol_win"])
        need = max(ma, vw) + 1
        close = ctx.history(self._risk, need, fields="close")[self._risk]
        fac = ctx.history(self._risk, need, fields="adj_factor")[self._risk].fillna(1.0)
        adj = close * fac
        if adj.isna().any() or len(adj) < need:
            return
        weight = 0.0
        if adj.iloc[-1] > adj.iloc[-ma:].mean():
            ret = adj.iloc[-vw:].pct_change().dropna()
            realized = float(ret.std()) * (252 ** 0.5) if len(ret) >= 2 else 99.0
            weight = min(1.0, float(self.params["target_vol"]) / realized) if realized > 0 else 1.0
        legs = [(self._risk, weight)]
        if self._def:
            legs.append((self._def, 1.0 - weight))
        for sym, w in sorted(legs, key=lambda t: t[1]):
            ctx.order_target_percent(sym, w)
