"""双动量大小盘轮动（Antonacci 式，适配 A 股 ETF）.

逻辑：每 rebalance 个交易日再平衡一次：
1. 计算各风险资产近 lookback 日复权动量（区间收益）；
2. 选动量最高者，只有其动量 > 防守资产同期动量（机会成本门槛）才持有；
3. 否则全部切防守资产（defensive 为空串时以 0 为门槛，纯空仓防守）。

非再平衡日重发上次目标（0.5% 死区抑制无效单，被拒腿自动自愈）；
卖单先于买单提交。
"""

from solidrock import Context, Strategy


class DualMomentum(Strategy):
    params = {
        "assets": "512100.SH,510300.SH",  # 风险资产池
        "defensive": "511010.SH",
        "lookback": 60,
        "rebalance": 20,
        "ma_filter": 0,  # >0 时胜者须在其 MA(ma_filter) 之上才可持有，否则回防守
    }

    def setup(self, ctx: Context) -> None:
        self._assets = [s.strip() for s in str(self.params["assets"]).split(",") if s.strip()]
        self._def = str(self.params["defensive"])
        ctx.universe = self._assets + ([self._def] if self._def else [])
        self._day = 0
        self._targets: dict[str, float] | None = None

    def _rebalance(self, ctx: Context) -> dict[str, float] | None:
        lb, rb = int(self.params["lookback"]), int(self.params["rebalance"])
        need = lb + 1
        mom: dict[str, float] = {}
        for sym in self._assets:
            close = ctx.history(sym, need, fields="close")[sym]
            fac = ctx.history(sym, need, fields="adj_factor")[sym].fillna(1.0)
            adj = close * fac  # 复权价动量（含分红/合并调整）
            if adj.isna().any() or len(adj) < need:
                return None  # 任一资产数据不足则本轮不动
            mom[sym] = adj.iloc[-1] / adj.iloc[0] - 1.0
        hurdle = 0.0
        if self._def:
            dclose = ctx.history(self._def, need, fields="close")[self._def]
            dfac = ctx.history(self._def, need, fields="adj_factor")[self._def].fillna(1.0)
            dadj = dclose * dfac
            if dadj.isna().any() or len(dadj) < need:
                return None
            hurdle = dadj.iloc[-1] / dadj.iloc[0] - 1.0
        winner = max(mom, key=mom.get)
        weights = {s: 0.0 for s in ctx.universe}
        ma_f = int(self.params.get("ma_filter", 0) or 0)
        eligible = mom[winner] > hurdle
        if eligible and ma_f > 0:
            c = ctx.history(winner, ma_f + 1, fields="close")[winner]
            f = ctx.history(winner, ma_f + 1, fields="adj_factor")[winner].fillna(1.0)
            a = c * f
            if a.isna().any() or len(a) < ma_f + 1:
                return None
            eligible = bool(a.iloc[-1] > a.iloc[-ma_f:].mean())
        if self._def:
            weights[winner if eligible else self._def] = 1.0
        elif eligible:
            weights[winner] = 1.0
        return weights

    def on_signal(self, ctx: Context) -> None:
        rb = int(self.params["rebalance"])
        self._day += 1
        if (self._day - 1) % rb == 0:
            self._targets = self._rebalance(ctx)
        if not self._targets:
            return
        for s, w in sorted(self._targets.items(), key=lambda kv: kv[1]):
            ctx.order_target_percent(s, w)
