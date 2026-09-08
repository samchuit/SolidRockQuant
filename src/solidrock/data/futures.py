"""期货合约规格与主连换月工具.

**合约规格**（乘数/保证金/费率/涨跌停）随交易所政策与合约月份变化，
``DEFAULT_SPECS`` 只收录常见品种的近似默认值（截至 2026-09 的常见水平，
仅供研究与回测起点，**不保证与实时合约参数一致**），生产使用请通过
``spec_overrides`` 按需覆盖。

**到期日**为近似值：商品期货取交割月 15 日、金融期货（中金所）取交割月
第三个周五，与真实最后交易日可能相差数日——回测中仅用于强制平仓触发，
不用于精确模拟交割。

**主连换月**（``roll_adjust_continuous``）：新浪主连为未复权拼接序列，
换月日价格跳变。比例复权法用价格跳变检测换月（阈值默认 5%），生成
``adj_factor`` 使 ``close × adj_factor`` 连续——与全库"原始价 + 因子"
的存储约定一致。属启发式方法（大幅波动可能被误判为换月），严谨研究
建议直接回测具体合约。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd

from solidrock.agent.errors import ErrorCode, err
from solidrock.data.symbols import parse_symbol

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class ContractSpec:
    """单个期货品种的合约规格."""

    product: str  # 品种代码，如 RB / IF
    multiplier: float  # 合约乘数（每手）
    margin_rate: float  # 保证金比例（单边）
    open_fee_rate: float  # 开仓费率（占合约价值）
    close_fee_rate: float  # 平昨费率
    close_today_fee_rate: float  # 平今费率
    limit_ratio: float  # 涨跌停幅度


# 常见品种近似默认值（费率/保证金随政策变动，请以交易所与期货公司实际为准）
DEFAULT_SPECS: dict[str, ContractSpec] = {
    "RB": ContractSpec("RB", 10, 0.13, 1e-4, 1e-4, 5e-4, 0.07),  # 螺纹钢
    "HC": ContractSpec("HC", 10, 0.13, 1e-4, 1e-4, 5e-4, 0.07),  # 热卷
    "I": ContractSpec("I", 100, 0.15, 1e-4, 1e-4, 5e-4, 0.08),  # 铁矿石
    "J": ContractSpec("J", 60, 0.20, 1.2e-4, 1.2e-4, 6e-4, 0.08),  # 焦炭
    "JM": ContractSpec("JM", 60, 0.20, 1.2e-4, 1.2e-4, 6e-4, 0.08),  # 焦煤
    "M": ContractSpec("M", 10, 0.08, 1.5e-4, 1.5e-4, 3e-4, 0.07),  # 豆粕
    "Y": ContractSpec("Y", 10, 0.08, 1.5e-4, 1.5e-4, 3e-4, 0.07),  # 豆油
    "TA": ContractSpec("TA", 5, 0.08, 3e-4, 3e-4, 6e-4, 0.06),  # PTA
    "MA": ContractSpec("MA", 10, 0.09, 3e-4, 3e-4, 6e-4, 0.07),  # 甲醇
    "SR": ContractSpec("SR", 10, 0.07, 3e-4, 3e-4, 6e-4, 0.06),  # 白糖
    "CU": ContractSpec("CU", 5, 0.10, 5e-5, 5e-5, 5e-5, 0.08),  # 铜
    "AU": ContractSpec("AU", 1000, 0.09, 5e-5, 5e-5, 1.5e-4, 0.08),  # 黄金
    "AG": ContractSpec("AG", 15, 0.10, 5e-5, 5e-5, 1.5e-4, 0.09),  # 白银
    "IF": ContractSpec("IF", 300, 0.12, 2.3e-5, 2.3e-5, 3.45e-4, 0.10),  # 沪深300股指
    "IH": ContractSpec("IH", 300, 0.12, 2.3e-5, 2.3e-5, 3.45e-4, 0.10),  # 上证50股指
    "IC": ContractSpec("IC", 200, 0.12, 2.3e-5, 2.3e-5, 3.45e-4, 0.10),  # 中证500股指
    "IM": ContractSpec("IM", 200, 0.12, 2.3e-5, 2.3e-5, 3.45e-4, 0.10),  # 中证1000股指
}


def _product(code: str) -> str:
    """品种代码 = 合约代码的字母部分（RB2505 → RB；主连 RB → RB）。"""
    match = re.match(r"([A-Za-z]+)", code)
    return match.group(1).upper() if match else code.upper()


def get_contract_spec(symbol: str, overrides: dict[str, dict] | None = None) -> ContractSpec:
    """取合约规格：``overrides``（品种或完整符号 → 字段字典）优先于默认表.

    overrides 示例::

        {"RB": {"margin_rate": 0.15}, "IF2506.CFE": {"multiplier": 300}}
    """
    sym = parse_symbol(symbol)
    if not sym.is_futures:
        raise err(
            ErrorCode.PARAM_INVALID,
            f"{symbol} 不是期货符号",
            hint="期货符号形如 RB2505.SHFE / IF2412.CFE / 主连 RB.SHFE",
        )
    product = _product(sym.code)
    base = DEFAULT_SPECS.get(product)
    if base is None:
        # 未知品种：保守的通用默认值（用户应显式覆盖）
        base = ContractSpec(product, 10, 0.12, 1e-4, 1e-4, 5e-4, 0.10)
    merged: dict = {
        "multiplier": base.multiplier,
        "margin_rate": base.margin_rate,
        "open_fee_rate": base.open_fee_rate,
        "close_fee_rate": base.close_fee_rate,
        "close_today_fee_rate": base.close_today_fee_rate,
        "limit_ratio": base.limit_ratio,
    }
    for key, value in (overrides or {}).items():
        target = _product(parse_symbol(key).code) if "." in key else key.upper()
        if target in (product, sym.code):
            merged.update({k: v for k, v in value.items() if k in merged})
    return ContractSpec(product=product, **merged)


def contract_expiry(symbol: str) -> pd.Timestamp | None:
    """合约近似到期日；主连（无数字）返回 None.

    商品期货 ≈ 交割月 15 日；中金所金融期货 ≈ 交割月第三个周五。
    CZCE 三位代码（TA505 → 2025-05），其余四位（RB2505 → 2025-05）。
    2000 年代以前的两位年份歧义按 20xx 处理。
    """
    sym = parse_symbol(symbol)
    digits = re.search(r"(\d+)$", sym.code)
    if digits is None:
        return None
    d = digits.group(1)
    year, month = (int(d[:2]) + 2000, int(d[2:])) if len(d) == 4 else (int(d[0]) + 2020, int(d[1:]))
    if not 1 <= month <= 12:
        raise err(
            ErrorCode.SYMBOL_INVALID,
            f"合约月份解析失败：{symbol}（month={month}）",
            hint="合约代码形如 RB2505（2025年5月）或 CZCE 三位码 TA505",
        )
    if sym.exchange == "CFE":
        # 交割月第三个周五
        first = pd.Timestamp(year=year, month=month, day=1)
        first_friday = first + pd.Timedelta(days=(4 - first.dayofweek) % 7)
        return first_friday + pd.Timedelta(weeks=2)
    return pd.Timestamp(year=year, month=month, day=15)


def roll_adjust_continuous(df: pd.DataFrame, threshold: float = 0.05) -> pd.DataFrame:
    """主连换月比例复权：检测价格跳变生成 ``adj_factor``，使 ``close × adj_factor`` 连续.

    输入为主连日线（含 date/close，按日期升序），输出追加/覆盖 ``adj_factor`` 列。
    阈值 ``threshold`` 为相邻两日收盘价变动比例的判定门限（换月跳变 vs 正常涨跌
    无法完全区分，属启发式；严谨研究请回测具体合约）。
    """
    out = df.copy()
    if "close" not in out.columns:
        raise err(ErrorCode.DATA_FORMAT_INVALID, "roll_adjust_continuous 需要 close 列")
    out = out.sort_values("date").reset_index(drop=True)
    close = out["close"].astype(float)
    factor = 1.0
    factors: list[float] = []
    prev_close: float | None = None
    for c in close:
        if prev_close is not None and prev_close > 0 and abs(c / prev_close - 1.0) > threshold:
            factor *= prev_close / c  # 换月：累积比例，使 hfq 连续
        factors.append(factor)
        prev_close = c
    out["adj_factor"] = factors
    return out
