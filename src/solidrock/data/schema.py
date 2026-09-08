"""标准数据 schema（全库唯一列名）.

日线 bars 标准列与单位约定：

==========  ================  =====================================================
列            类型              说明
==========  ================  =====================================================
symbol      str               统一符号（见 symbols.py）
date        datetime64[ns]    交易日（零点整）
open        float64           开盘价（元）
high        float64           最高价（元）
low         float64           最低价（元）
close       float64           收盘价（元）
pre_close   float64           昨收盘（元，**除权后口径**：与交易所公布一致）
volume      float64           成交量（手，股票与期货均如此）
amount      float64           成交额（元）
turnover_rate float64         换手率（%，源样即存）
adj_factor  float64           复权因子（后复权口径，见下）
suspended   bool              当日是否停牌（日线源通常只含交易日，暂恒为 False）
settle      float64           结算价（元，期货专用，可空）
open_interest float64         持仓量（手，期货专用，可空）
==========  ================  =====================================================

复权因子约定（与 Tushare 一致）：

- ``hfq_close = close * adj_factor``（后复权，上市首日因子 ≈ 1，随分红送配累乘）；
- ``qfq_close = hfq_close / latest_adj_factor``（最新交易日因子归一）；
- 历史因子值不随新除权事件改变，因此 **存原始价 + 因子** 可保证数据版本稳定
  （前复权价会随每次除权漂移，破坏可复现性，故不落库）；
- 指数/期货无复权概念，因子恒为 1.0；无法提供因子时为 NaN（如适配器关闭该选项）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from solidrock.agent.errors import ErrorCode, err

# 校验失败时 OHLC 逻辑检查允许的相对误差（浮点容差）
_OHLC_TOL = 1e-6


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    name: str
    dtype: str  # pandas/numpy dtype 字符串
    nullable: bool
    required: bool  # 必须存在的列（其余列存在与否均可）
    description: str


DAILY_BAR_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("symbol", "object", False, True, "统一符号"),
    ColumnSpec("date", "datetime64[ns]", False, True, "交易日"),
    ColumnSpec("open", "float64", False, True, "开盘价（元）"),
    ColumnSpec("high", "float64", False, True, "最高价（元）"),
    ColumnSpec("low", "float64", False, True, "最低价（元）"),
    ColumnSpec("close", "float64", False, True, "收盘价（元）"),
    ColumnSpec("pre_close", "float64", True, True, "昨收（除权后口径，元）"),
    ColumnSpec("volume", "float64", True, True, "成交量（手）"),
    ColumnSpec("amount", "float64", True, True, "成交额（元）"),
    ColumnSpec("turnover_rate", "float64", True, True, "换手率（%）"),
    ColumnSpec("adj_factor", "float64", True, True, "复权因子（后复权口径）"),
    ColumnSpec("suspended", "bool", False, True, "是否停牌"),
    ColumnSpec("settle", "float64", True, False, "结算价（元，期货）"),
    ColumnSpec("open_interest", "float64", True, False, "持仓量（手，期货）"),
)

DAILY_BAR_COLUMN_NAMES: tuple[str, ...] = tuple(c.name for c in DAILY_BAR_COLUMNS)
REQUIRED_BAR_COLUMNS: frozenset[str] = frozenset(c.name for c in DAILY_BAR_COLUMNS if c.required)
# 增量更新时"新数据为 NaN 则保留旧值"的列（价格/成交量以新数据为准）
COALESCE_COLUMNS: tuple[str, ...] = ("pre_close", "adj_factor", "turnover_rate", "amount")

# 复权时应用因子的价格列
ADJUSTED_PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "pre_close")


def empty_bars_frame() -> pd.DataFrame:
    """返回符合标准 schema 的空 DataFrame（列名与 dtype 就位）。"""
    data: dict[str, object] = {}
    for col in DAILY_BAR_COLUMNS:
        data[col.name] = pd.Series(dtype=col.dtype)
    return pd.DataFrame(data)


def validate_bars(df: pd.DataFrame) -> pd.DataFrame:
    """校验并规范化为标准 schema；不合格抛 ``DATA_FORMAT_INVALID``（带 hint）.

    做的事：检查必需列、转换 dtype（date 接受 date/str、数值列宽转换）、
    排序（symbol, date）、重置索引；并做硬性 OHLC 逻辑检查（high < low 直接判坏数据）。
    """
    if not isinstance(df, pd.DataFrame):
        raise err(
            ErrorCode.DATA_FORMAT_INVALID,
            f"预期 pandas DataFrame，收到 {type(df).__name__}",
            hint="数据源适配器应返回 DataFrame",
        )
    if df.empty:
        # 空帧无需校验（统一返回带标准列的空表）；调用方自行判断 empty
        return empty_bars_frame()
    missing = REQUIRED_BAR_COLUMNS - set(df.columns)
    if missing:
        raise err(
            ErrorCode.DATA_FORMAT_INVALID,
            f"缺少必需列：{sorted(missing)}",
            hint=f"标准日线 schema 要求列：{list(DAILY_BAR_COLUMN_NAMES)}，适配器负责把源列名映射到标准列",
        )

    out = df.copy()
    # 先补齐缺失的标准列占位（后续 dtype 转换依赖列存在）
    for col in DAILY_BAR_COLUMNS:
        if col.name not in out.columns:
            if col.dtype == "bool":
                out[col.name] = False
            elif col.dtype == "float64":
                out[col.name] = np.nan
            elif col.dtype == "datetime64[ns]":
                out[col.name] = pd.NaT
            else:
                out[col.name] = pd.NA
    # symbol
    out["symbol"] = out["symbol"].astype(str)
    # date：接受 date / str / datetime
    try:
        out["date"] = pd.to_datetime(out["date"])
    except (ValueError, TypeError) as exc:
        raise err(
            ErrorCode.DATA_FORMAT_INVALID,
            f"date 列无法解析为日期：{exc}",
            hint="date 列应为 datetime/date/ISO 字符串",
        ) from exc
    if out["date"].isna().any():
        bad = int(out["date"].isna().sum())
        raise err(
            ErrorCode.DATA_FORMAT_INVALID,
            f"date 列存在 {bad} 个无法解析的值",
            hint="检查源数据日期格式是否统一",
        )
    out["date"] = out["date"].dt.normalize()
    # 归一到 ns：pandas 3 的 to_datetime 默认 us，跨单位比较在 pandas 下会出问题
    out["date"] = out["date"].astype("datetime64[ns]")
    # 数值列
    for col in DAILY_BAR_COLUMNS:
        if col.dtype == "float64":
            converted = pd.to_numeric(out[col.name], errors="coerce")
            new_nan = int(converted.isna().sum() - out[col.name].isna().sum())
            if new_nan > 0:
                raise err(
                    ErrorCode.DATA_FORMAT_INVALID,
                    f"列 {col.name!r} 有 {new_nan} 个无法转为数值的值",
                    hint="源数据中混入了非数值内容（如 '--'），适配器应先清洗",
                )
            out[col.name] = converted.astype("float64")
        elif col.dtype == "bool":
            out[col.name] = out[col.name].fillna(False).astype(bool)
    # 键列不允许缺失
    for key in ("symbol", "date", "open", "high", "low", "close"):
        if out[key].isna().any():
            n = int(out[key].isna().sum())
            raise err(
                ErrorCode.DATA_FORMAT_INVALID,
                f"关键列 {key!r} 存在 {n} 个缺失值",
                hint="symbol/date/OHLC 是硬性字段，缺失行应剔除或修复",
            )
    # OHLC 硬性逻辑：low <= min(open, close) <= max(open, close) <= high
    bad_ohlc = out["high"] + _OHLC_TOL < out["low"]
    bad_hi = out[["open", "close"]].max(axis=1) > out["high"] + _OHLC_TOL
    bad_lo = out[["open", "close"]].min(axis=1) < out["low"] - _OHLC_TOL
    n_bad = int((bad_ohlc | bad_hi | bad_lo).sum())
    if n_bad:
        sample = out.loc[bad_ohlc | bad_hi | bad_lo, ["symbol", "date"]].head(3).to_dict("records")
        raise err(
            ErrorCode.DATA_FORMAT_INVALID,
            f"OHLC 逻辑错误（high<low 或 OHLC 越界）共 {n_bad} 行",
            hint="多为源数据损坏，请检查数据源或剔除坏行",
            details={"sample": sample},
        )
    out = out.sort_values(["symbol", "date"], kind="stable").reset_index(drop=True)
    return out[list(DAILY_BAR_COLUMN_NAMES)]
