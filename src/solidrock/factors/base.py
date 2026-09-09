"""因子基座：面板容器与因子基类.

因子计算输入是**多符号宽表**（index=date, columns=symbol），与回测的
长表（标准 schema）互转。因子只读数据，返回同样形状的宽表（因子值）::

    class Momentum20(Factor):
        params = {"n": 20}
        lookback = 20                     # 计算需要的历史 bar 数（数据加载 warmup 依据）

        def compute(self, data: FactorData) -> pd.DataFrame:
            return data.hfq_close() / data.hfq_close().shift(int(self.params["n"])) - 1
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd

from solidrock.agent.errors import ErrorCode, err
from solidrock.utils.loader import load_class_from_file

_REQUIRED_FIELDS = ("close", "adj_factor")
_PANEL_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
    "adj_factor",
)


@dataclass
class FactorData:
    """因子计算输入：标准字段的多符号宽表（原始价口径，含复权因子）."""

    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    pre_close: pd.DataFrame
    volume: pd.DataFrame
    amount: pd.DataFrame
    adj_factor: pd.DataFrame
    extra: dict[str, pd.DataFrame] = field(default_factory=dict)

    @classmethod
    def from_bars(cls, df: pd.DataFrame) -> FactorData:
        """标准 schema 长表 → 宽表面板.

        ``dropna=False``：某符号整列 NaN（如新浪回退通道无复权因子）也保留列，
        否则后续乘法按列对齐时会把该符号整体变 NaN。
        """
        if df.empty:
            raise err(
                ErrorCode.NO_DATA,
                "因子计算的输入数据为空",
                hint="先执行 fetch_bars 更新数据",
            )
        kwargs: dict[str, pd.DataFrame] = {}
        for name in _PANEL_FIELDS:
            kwargs[name] = df.pivot_table(
                index="date", columns="symbol", values=name, aggfunc="last", dropna=False
            ).sort_index()
        return cls(**kwargs)

    def hfq_close(self) -> pd.DataFrame:
        """后复权收盘（因子计算/收益率用，跨除权连续）.

        复权因子缺失（如新浪 ETF 回退通道不提供因子）按 1.0 处理，
        即该标的用原始价近似——与 load_bars/引擎基准序列的约定一致。
        """
        return self.close * self.adj_factor.fillna(1.0)

    def __getitem__(self, name: str) -> pd.DataFrame:
        """便捷访问：标准字段或 extra 字段。"""
        if name in _PANEL_FIELDS:
            return getattr(self, name)
        if name in self.extra:
            return self.extra[name]
        raise err(
            ErrorCode.PARAM_INVALID,
            f"字段 {name!r} 不存在于 FactorData",
            hint=f"可用字段：{_PANEL_FIELDS} 或 extra 中自定义字段",
        )


class Factor:
    """因子基类。子类声明 ``params`` 与 ``lookback``，实现 ``compute``。"""

    params: dict[str, Any] = {}  # 公共 API：类级参数声明（与 Strategy.params 同约定）
    lookback: ClassVar[int] = 0  # 计算所需的最少历史 bar 数

    def __init__(self, **param_overrides: Any) -> None:
        unknown = set(param_overrides) - set(type(self).params)
        if unknown:
            raise err(
                ErrorCode.PARAM_INVALID,
                f"因子 {type(self).__name__} 未声明参数：{sorted(unknown)}",
                hint=f"可用参数：{sorted(type(self).params)}",
            )
        self.params = {**type(self).params, **param_overrides}

    def compute(self, data: FactorData) -> pd.DataFrame:
        """计算因子值：输入面板，输出宽表（index=date, columns=symbol）。"""
        raise NotImplementedError

    @property
    def name(self) -> str:
        return type(self).__name__


# ---------------------------------------------------------------- 因子注册表
_FACTOR_REGISTRY: dict[str, type[Factor]] = {}


def register_factor(cls: type[Factor]) -> type[Factor]:
    """类装饰器：把 Factor 子类注册到因子库（插件接入点之一）.

    注册名取 ``cls.__name__``；重名注册直接报错（避免静默覆盖）。
    """
    name = cls.__name__
    existing = _FACTOR_REGISTRY.get(name)
    if existing is not None and existing is not cls:
        raise ValueError(f"因子名 {name!r} 已被 {existing.__module__}.{existing.__qualname__} 注册")
    _FACTOR_REGISTRY[name] = cls
    return cls


def create_factor(name: str, **params: Any) -> Factor:
    """按注册名实例化因子（内置 + entry-points 第三方插件）。"""
    cls = _FACTOR_REGISTRY.get(name)
    if cls is None:
        from solidrock.plugins import require_discovered

        require_discovered("factors")  # 首次未命中：扫描第三方插件后重试
        cls = _FACTOR_REGISTRY.get(name)
    if cls is None:
        raise err(
            ErrorCode.SOURCE_NOT_REGISTERED,
            f"未注册的因子 {name!r}",
            hint=f"已注册因子：{sorted(_FACTOR_REGISTRY)}；第三方因子可通过 entry-points 组 solidrock.factors 接入",
        )
    return cls(**params)


def list_registered_factors() -> list[str]:
    """全部已注册因子名（排序）。"""
    return sorted(_FACTOR_REGISTRY)


def load_factor_class(path: str | Path) -> type[Factor]:
    """加载因子文件中的 Factor 子类（文件内应只定义一个）。"""
    return load_class_from_file(path, Factor, "因子")
