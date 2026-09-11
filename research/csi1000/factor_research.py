"""中证1000成分股截面因子检验：RankIC + 分层（日频前瞻）.

因子族（价格量数据可得范围内）：
- 动量: MOM20 / MOM60 / MOM120（后复权区间收益）
- 反转: REV5（5 日收益取负）
- 低波: VOL20（20 日日收益波动取负，低波异象）
- 低振幅: AMP20（20 日均振幅取负）
- 非流动性: ILLIQ20（Amihud |ret|/amount 均值，高值=流动性差）

口径：
- 收益与因子用后复权收盘（close×adj_factor，腾讯 hfq 推得，精确连续）；
- 日频 RankIC（Spearman），分层 5 层等权；
- 注意：宇宙为**当前成分**（2016-2026 回测），存在幸存者偏差，
  结论看分层单调性与多空结构，多头绝对收益仅作参考。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from solidrock.data.store import DataStore  # noqa: E402
from solidrock.factors.analysis import analyze_factor  # noqa: E402
from solidrock.factors.base import Factor, FactorData  # noqa: E402

STORE = DataStore(ROOT / ".solidrock")
CONS_FILE = Path(__file__).parent / "cons_current.csv"
START, END = "2016-01-04", "2026-09-08"


def _hfq(data: FactorData) -> pd.DataFrame:
    return data.hfq_close()


def _make_factor(name: str, fn) -> Factor:
    """按因子名生成 Factor 子类实例（compute 委托 fn(FactorData)）."""

    class _PanelFactor(Factor):
        def compute(self, data: FactorData) -> pd.DataFrame:
            return fn(data)

        @property
        def name(self) -> str:  # type: ignore[override]
            return name

    return _PanelFactor()


def make_factors() -> list[Factor]:
    fns: dict[str, object] = {
        "MOM20": lambda d: _hfq(d) / _hfq(d).shift(20) - 1.0,
        "MOM60": lambda d: _hfq(d) / _hfq(d).shift(60) - 1.0,
        "MOM120": lambda d: _hfq(d) / _hfq(d).shift(120) - 1.0,
        "REV5": lambda d: -(_hfq(d) / _hfq(d).shift(5) - 1.0),
        "REV20": lambda d: -(_hfq(d) / _hfq(d).shift(20) - 1.0),
        "VOL20": lambda d: -_hfq(d).pct_change().rolling(20).std(),
        "AMP20": lambda d: -((d.high / d.low - 1.0).rolling(20).mean()),
        "ILLIQ20": lambda d: (_hfq(d).pct_change().abs() / d.amount).rolling(20).mean() * 1e9,
    }
    return [_make_factor(name, fn) for name, fn in fns.items()]


def main() -> None:
    universe = pd.read_csv(CONS_FILE)["symbol"].tolist()
    rows = []
    details: dict[str, dict] = {}
    for f in make_factors():
        try:
            res = analyze_factor(f, STORE, universe, start=START, end=END, quantiles=5,
                                 fwd_period=1, min_stocks=300, log_experiment=False)
        except Exception as e:  # noqa: BLE001
            print(f"{f.name}: FAILED {type(e).__name__} {str(e)[:80]}")
            continue
        icm = res.ic_summary
        ls = res.layer_stats.loc["L-S"]
        rows.append({
            "factor": f.name,
            "ic_mean": icm.get("ic_mean"),
            "icir": icm.get("ic_ir"),
            "t_stat": icm.get("ic_t_stat"),
            "ic_pos": icm.get("positive_ratio"),
            "ls_annual": ls.get("annual_return"),
            "ls_sharpe": ls.get("sharpe"),
            "q1_annual": res.layer_stats.loc["1", "annual_return"],
            "q5_annual": res.layer_stats.loc["5", "annual_return"],
            "autocorr": res.factor_autocorr,
        })
        details[f.name] = {
            "ic": res.ic,
            "layer_stats": res.layer_stats,
            "layer_navs": res.layer_navs,
        }
        print(f"{f.name}: IC={icm.get('ic_mean'):.4f} ICIR={icm.get('ic_ir'):.2f} "
              f"t={icm.get('ic_t_stat'):.1f} L-S年化={ls.get('annual_return'):.2%} "
              f"自相关={res.factor_autocorr:.2f}")
    df = pd.DataFrame(rows).set_index("factor")
    out = Path(__file__).parent / "factor_ic.csv"
    df.to_csv(out, encoding="utf-8-sig")
    print("\n=== 汇总（按 |ICIR| 排序）===")
    print(df.assign(abs_icir=df["icir"].abs()).sort_values("abs_icir", ascending=False)
          .drop(columns=["abs_icir"]).round(4).to_string())
    print(f"\n已写出: {out}")

    # 分层单调性：各层年化收益的秩相关（因子方向单调性检查）
    print("\n=== 分层年化（1=因子最低组，5=最高组）===")
    for name, d in details.items():
        col = d["layer_stats"].loc[[str(i) for i in range(1, 6)], "annual_return"]
        print(f"{name}: " + " ".join(f"Q{i}={v:+.1%}" for i, v in col.items()))


if __name__ == "__main__":
    main()
