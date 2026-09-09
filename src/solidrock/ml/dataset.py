"""ML 量化管道：点时特征矩阵 → walk-forward 训练预测 → 信号→回测.

ML 量化的最大风险是**前视偏差**和**过拟合**。本模块的防线：
- 点时纪律内建于 ``MLDataset``：t 日的特征只用 ≤t 的因子值，
  标签 = t → t+horizon 的前瞻收益；特征矩阵不含未来信息；
- walk-forward 切分严格按时间顺序：训练窗口在前、预测窗口在后，
  禁止 shuffle；
- 预测 IC 在**未见过的测试窗口**上计算，训练 IC 与测试 IC 的差距
  即过拟合度量。

依赖：``[ml]`` extras（lightgbm + scikit-learn），惰性导入。
"""

from __future__ import annotations

import pandas as pd

from solidrock.agent.errors import ErrorCode, err


def stack_panels(panels: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """宽表面板组（name → date×symbol）堆叠为长表.

    返回 index=(date, symbol)、columns=因子名的 DataFrame。
    """
    if not panels:
        raise err(ErrorCode.PARAM_INVALID, "panels 为空")
    stacked = pd.concat(
        {name: panel.stack() for name, panel in panels.items()},
        axis=1,
    )
    stacked.index.names = ["date", "symbol"]
    return stacked


def build_dataset(
    factor_values: dict[str, pd.DataFrame],
    close_hfq: pd.DataFrame,
    *,
    horizon: int = 5,
) -> tuple[pd.DataFrame, pd.Series]:
    """构建点时特征矩阵与前瞻收益标签.

    - 特征：t 日的各因子截面值（堆叠为 (date, symbol) 长表）；
    - 标签：t → t+horizon 的后复权收益（跨除权连续）。

    返回 (X, y)：X 的 index = (date, symbol)，columns = 因子名；
    含 NaN 的行自动剔除（因子缺失或收益无法计算）。
    """
    if horizon < 1:
        raise err(ErrorCode.PARAM_INVALID, f"horizon 应 >= 1，收到 {horizon}")
    X = stack_panels(factor_values)
    fwd = close_hfq.shift(-horizon) / close_hfq - 1.0
    y = stack_panels({"__label__": fwd})
    y = y["__label__"]
    joined = X.join(y.rename("__label__"), how="inner").dropna()
    if joined.empty:
        raise err(
            ErrorCode.NO_DATA,
            "特征矩阵为空：因子值与前瞻收益无有效重叠",
            hint="检查因子 lookback 是否覆盖数据起点、horizon 是否合理",
        )
    labels = joined["__label__"]
    features = joined.drop(columns=["__label__"])
    return features, labels


def walk_forward_splits(
    dates: pd.DatetimeIndex,
    *,
    train_window: int,
    test_window: int,
    step: int,
) -> list[tuple[pd.DatetimeIndex, pd.DatetimeIndex]]:
    """生成 walk-forward (训练日集合, 测试日集合) 序列.

    严格按时间顺序：训练窗口在前、测试窗口紧跟其后，
    每次向未来滚动 ``step`` 个交易日。无 shuffle、无重叠泄漏。
    """
    if train_window < 1 or test_window < 1 or step < 1:
        raise err(
            ErrorCode.PARAM_INVALID,
            f"train_window/test_window/step 均应 >= 1，收到 {train_window}/{test_window}/{step}",
        )
    unique_dates = sorted(set(dates))
    splits: list[tuple[pd.DatetimeIndex, pd.DatetimeIndex]] = []
    pos = 0
    n = len(unique_dates)
    while True:
        train_end = pos + train_window
        test_end = train_end + test_window
        if test_end > n:
            break
        train = pd.DatetimeIndex(unique_dates[pos:train_end])
        test = pd.DatetimeIndex(unique_dates[train_end:test_end])
        splits.append((train, test))
        pos += step
    if not splits:
        raise err(
            ErrorCode.NO_DATA,
            f"数据不足：{n} 个交易日无法支撑 train={train_window} + test={test_window} 的 walk-forward",
            hint="增大数据区间或减小 train_window/test_window",
        )
    return splits
