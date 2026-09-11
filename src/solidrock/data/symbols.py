"""统一符号规范与解析.

全库唯一的符号格式（Tushare/Wind 式后缀约定，国内事实标准）::

    A股/指数/ETF:  000001.SZ   600519.SH   000300.SH   510300.SH   830799.BJ
    期货合约:      RB2505.SHFE   IF2412.CFE   TA505.CZCE
    期货主连:      RB.SHFE

规则：
- 大小写不敏感，解析后统一规范化（品种代码与交易所后缀均转大写）；
- ``asset_type`` 为**尽力推断**（用于展示与路由），数据源适配器才是权威；
- 格式非法抛 ``SYMBOL_INVALID``，hint 中给出正确示例。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from solidrock.agent.errors import ErrorCode, err

STOCK_EXCHANGES = frozenset({"SH", "SZ", "BJ"})
FUTURES_EXCHANGES = frozenset({"SHFE", "DCE", "CZCE", "CFE", "INE", "GFEX"})
# 海外交易所（供数据源插件使用，如 yfinance；事件回测规则暂只覆盖 A股/期货）
OVERSEAS_EXCHANGES = frozenset({"NYSE", "NASDAQ", "AMEX", "HKEX", "TSE", "LSE"})
ALL_EXCHANGES = STOCK_EXCHANGES | FUTURES_EXCHANGES | OVERSEAS_EXCHANGES

VALID_SYMBOL_EXAMPLES = "000001.SZ / 600519.SH / 000300.SH / 510300.SH / RB2505.SHFE / RB.SHFE / AAPL.NASDAQ"

# 深交所 T+0 ETF（跨境/黄金/商品期货等，无干净代码段规则，列出常见品种；非穷举，以交易所最新规则为准）
SZ_T0_ETFS = frozenset(
    {
        "159920",  # 恒生 ETF（跨境）
        "159941",  # 纳指 ETF（跨境）
        "159934",  # 易方达黄金 ETF
        "159937",  # 博时黄金 ETF
        "159942",  # 华夏黄金 ETF
        "159980",  # 有色期货 ETF
        "159985",  # 豆粕期货 ETF
        "159981",  # 能源化工期货 ETF
    }
)
# 沪市 T+0 ETF 代码段：511 债券/货币、513 跨境、518 黄金
SH_T0_ETF_PREFIXES = frozenset({"511", "513", "518"})


class AssetType(str, Enum):
    """资产类型（数据源适配器为权威，此处的推断仅用于展示与路由）。"""

    STOCK = "stock"
    INDEX = "index"
    ETF = "etf"
    FUTURES = "futures"  # 具体合约
    FUTURES_CONTINUOUS = "futures_continuous"  # 主力连续（主连）


@dataclass(frozen=True, slots=True)
class Symbol:
    """解析后的统一符号。"""

    code: str  # 点号前部分，如 000001 / RB2505 / RB
    exchange: str  # 大写交易所后缀，如 SZ / SHFE
    asset_type: AssetType | None  # 尽力推断，可能为 None

    @property
    def value(self) -> str:
        return f"{self.code}.{self.exchange}"

    @property
    def is_futures(self) -> bool:
        return self.exchange in FUTURES_EXCHANGES

    @property
    def is_t0(self) -> bool:
        """是否 T+0 回转交易（尽力推断，撮合层的 T+1 约束依据）.

        期货天然 T+0；股票/北交所 T+1；ETF 按品种：沪市 511/513/518 段
        （债券/货币、跨境、黄金）与深市常见跨境/黄金/商品 ETF 为 T+0，
        其余 ETF T+1。列表非穷举，以交易所最新规则为准。
        """
        if self.is_futures:
            return True
        if self.asset_type is not AssetType.ETF:
            return False
        if self.exchange == "SH" and self.code[:3] in SH_T0_ETF_PREFIXES:
            return True
        return self.code in SZ_T0_ETFS

    def __str__(self) -> str:
        return self.value


def infer_asset_type(code: str, exchange: str) -> AssetType | None:
    """按代码前缀尽力推断资产类型；无法判断返回 None。

    规则依据各交易所代码段划分：
    - 上交所：60/68 股票，51/50/56/58 基金（ETF/LOF），000/88x 等为指数；
    - 深交所：399 指数，15/16/18 基金，其余 00/30 为股票；
    - 北交所：899 指数（如北证50 899050），其余为股票；
    - 期货交易所：纯字母为主连，字母+数字为具体合约。
    """
    if exchange in FUTURES_EXCHANGES:
        return AssetType.FUTURES_CONTINUOUS if code.isalpha() else AssetType.FUTURES
    if exchange == "SH":
        if code[:2] in {"50", "51", "56", "58"}:
            return AssetType.ETF
        if code[:2] in {"60", "68"}:
            return AssetType.STOCK
        return AssetType.INDEX
    if exchange == "SZ":
        if code[:3] == "399":
            return AssetType.INDEX
        if code[:2] in {"15", "16", "18"}:
            return AssetType.ETF
        return AssetType.STOCK
    if exchange == "BJ":
        return AssetType.INDEX if code[:3] == "899" else AssetType.STOCK
    if exchange in OVERSEAS_EXCHANGES:
        return AssetType.STOCK  # 海外现货统一按股票处理（数据源插件负责具体语义）
    return None


def _validate_code(code: str, exchange: str) -> str:
    if exchange in FUTURES_EXCHANGES:
        # 期货：1-2 位字母 + 3~4 位数字（CZCE 为 3 位年份码，其余 4 位），或纯字母主连
        if code.isalpha():
            return code  # 主连
        if re.fullmatch(r"[A-Z]{1,2}\d{3,4}", code) is None:
            raise err(
                ErrorCode.SYMBOL_INVALID,
                f"期货代码 {code!r} 格式非法",
                hint=f"期货合约形如 RB2505.SHFE / TA505.CZCE，主连形如 RB.SHFE。合法示例：{VALID_SYMBOL_EXAMPLES}",
            )
        return code
    if exchange in OVERSEAS_EXCHANGES:
        # 海外：字母数字混合 1~6 位（AAPL / 7203 / BRK.B 不支持含点二级后缀）
        if re.fullmatch(r"[A-Z0-9]{1,6}", code) is None:
            raise err(
                ErrorCode.SYMBOL_INVALID,
                f"海外标的代码 {code!r} 格式非法",
                hint=f"海外符号形如 AAPL.NASDAQ / 7203.TSE（1~6 位字母数字）。合法示例：{VALID_SYMBOL_EXAMPLES}",
            )
        return code
    # 股票/指数/ETF：6 位数字
    if len(code) != 6 or not code.isdigit():
        raise err(
            ErrorCode.SYMBOL_INVALID,
            f"代码 {code!r} 应为 6 位数字",
            hint=f"A股/指数/ETF 代码为 6 位数字 + 交易所后缀。合法示例：{VALID_SYMBOL_EXAMPLES}",
        )
    return code


def parse_symbol(raw: str) -> Symbol:
    """解析统一符号；非法输入抛 ``SYMBOL_INVALID``（带修复 hint）。"""
    if not isinstance(raw, str) or not raw.strip():
        raise err(
            ErrorCode.SYMBOL_INVALID,
            "符号不能为空",
            hint=f"符号格式为 <代码>.<交易所>，合法示例：{VALID_SYMBOL_EXAMPLES}",
        )
    text = raw.strip().upper()
    if text.count(".") != 1:
        raise err(
            ErrorCode.SYMBOL_INVALID,
            f"符号 {raw!r} 缺少交易所后缀（应恰好包含一个 '.'）",
            hint=f"例如 000001 应写成 000001.SZ。合法示例：{VALID_SYMBOL_EXAMPLES}",
        )
    code, exchange = text.split(".")
    code, exchange = code.strip(), exchange.strip()
    if exchange not in ALL_EXCHANGES:
        raise err(
            ErrorCode.SYMBOL_INVALID,
            f"未知交易所后缀 {exchange!r}",
            hint=f"支持的交易所：{', '.join(sorted(ALL_EXCHANGES))}。合法示例：{VALID_SYMBOL_EXAMPLES}",
        )
    code = _validate_code(code, exchange)
    return Symbol(code=code, exchange=exchange, asset_type=infer_asset_type(code, exchange))


def normalize_symbol(raw: str) -> str:
    """解析并返回规范化符号字符串。"""
    return parse_symbol(raw).value


def validate_symbols(raws: list[str]) -> list[Symbol]:
    """批量解析；任一非法即抛错（details 中带出错位置）。"""
    return [parse_symbol(raw) for raw in raws]


def to_source_code(symbol: Symbol, style: str) -> str:
    """将统一符号转换为各数据源的本地代码格式.

    - ``akshare_em``：东方财富系接口（股票/ETF/指数），纯代码，如 ``000001``；
    - ``akshare_sina``：新浪期货，合约如 ``RB2505``，主连加 0 后缀如 ``RB0``；
    - ``tushare``：与统一符号相同，如 ``000001.SZ``。
    """
    if style == "tushare":
        return symbol.value
    if style == "akshare_em":
        return symbol.code
    if style == "akshare_sina":
        if not symbol.is_futures:
            raise err(
                ErrorCode.PARAM_INVALID,
                f"新浪期货代码仅支持期货符号，收到 {symbol.value}",
                hint="期货符号形如 RB2505.SHFE 或主连 RB.SHFE",
            )
        return symbol.code if symbol.asset_type is AssetType.FUTURES else f"{symbol.code}0"
    raise err(
        ErrorCode.PARAM_INVALID,
        f"未知代码风格 {style!r}",
        hint="支持：akshare_em / akshare_sina / tushare",
    )
