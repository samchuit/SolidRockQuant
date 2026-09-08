"""20 日动量因子示例.

数据准备::

    srq data update --symbols 000001.SZ,600519.SH,... --start 2023-01-01

运行分析（股票池越大截面越可信，建议 50 只以上）::

    srq factor analyze examples/momentum_factor.py -u 000001.SZ,600519.SH,300750.SZ,... \
        --start 2024-01-01 --end 2025-12-31 --param n=20
"""

from solidrock.factors import Factor, FactorData


class Momentum20(Factor):
    params = {"n": 20}
    lookback = 20

    def compute(self, data: FactorData):
        n = int(self.params["n"])
        hfq = data.hfq_close()
        return hfq / hfq.shift(n) - 1
