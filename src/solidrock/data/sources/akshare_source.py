"""AKShare 适配器（默认数据源，免费、无需 token）.

覆盖：A股/指数/ETF 日线、期货合约与主连日线、交易日历、股票列表、中证指数成分。

**双通道回退**：东财系接口为主通道，新浪系为回退通道。东财对数据中心 IP
的封锁/限流是常态，主通道请求失败时自动切换新浪（北交所无新浪数据，直接报错）。

单位换算（源 → 标准schema）：
- 东财系 stock_zh_a_hist / fund_etf_hist_em / index_zh_a_hist：
  成交量（手）原样、成交额（元）原样、换手率（%）原样、
  pre_close = close - 涨跌额（除权后口径，精确）；
- 新浪系 stock_zh_a_daily / fund_etf_hist_sina：成交量（**股**）÷100 → 手、
  换手率（**小数**）×100 → %、pre_close 用复权因子比值法推除权昨收；
- 新浪系期货 futures_zh_daily_sina / futures_main_sina：成交量（手）原样、
  无成交额（NaN）、pre_close = close.shift(1)。
  注意 akshare 版本差异：新版合约接口为英文列（hold/settle），
  旧版与主连接口为中文列，两者都做了映射。

复权因子：股票/ETF 由后复权收盘推得 ``adj_factor = hfq_close / close``
（与 Tushare 因子同为自上市累乘口径）；指数/期货恒为 1.0；新浪 ETF 无后复权 → NaN。
"""

from __future__ import annotations

import importlib.util
from typing import Any

import numpy as np
import pandas as pd

from solidrock.agent.errors import ErrorCode, SolidRockError, err
from solidrock.data.schema import DAILY_BAR_COLUMN_NAMES
from solidrock.data.sources.base import Capability, DataSource
from solidrock.data.sources.registry import register_source
from solidrock.data.symbols import AssetType, Symbol, parse_symbol, to_source_code

# 东方财富系通用列名映射（源列 → 标准列）
_EM_RENAME: dict[str, str] = {
    "日期": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
    "涨跌额": "change",
    "换手率": "turnover_rate",
}
# 新浪系期货映射：兼容新版英文列与旧版中文列
_SINA_FUT_RENAME: dict[str, str] = {
    "日期": "date",
    "开盘价": "open",
    "最高价": "high",
    "最低价": "low",
    "收盘价": "close",
    "hold": "open_interest",
    "持仓量": "open_interest",
    "settle": "settle",
    "结算价": "settle",
    "动态结算价": "dynamic_settle",
}

# 新浪个股回退时向前多取的日历天数（用于计算复权因子比值与除权昨收）
_SINA_PAD_DAYS = 45


def _ensure_ak() -> Any:
    """惰性导入 akshare；缺库时给安装 hint。"""
    if importlib.util.find_spec("akshare") is None:
        raise err(
            ErrorCode.SOURCE_UNAVAILABLE,
            "数据源 akshare 的依赖库未安装",
            hint="pip install 'solidrock-quant[sources]' 或 pip install akshare",
        )
    import akshare as ak

    return ak


@register_source
class AkshareSource(DataSource):
    name = "akshare"
    capabilities = frozenset(
        {
            Capability.BARS_DAILY_STOCK,
            Capability.BARS_DAILY_INDEX,
            Capability.BARS_DAILY_ETF,
            Capability.BARS_DAILY_FUTURES,
            Capability.BARS_DAILY_FUTURES_CONTINUOUS,
            Capability.CALENDAR,
            Capability.INSTRUMENTS_STOCK,
            Capability.INDEX_CONSTITUENTS,
        }
    )

    @classmethod
    def is_available(cls) -> bool:
        return importlib.util.find_spec("akshare") is not None

    # ------------------------------------------------------------------ 日线
    def _fetch_bars_one(
        self,
        symbol: Symbol,
        start: pd.Timestamp | None,
        end: pd.Timestamp | None,
        *,
        with_adj_factor: bool,
    ) -> pd.DataFrame:
        ak = _ensure_ak()
        t = symbol.asset_type
        if t is AssetType.STOCK:
            return self._fetch_stock(ak, symbol, start, end, with_adj_factor=with_adj_factor)
        if t is AssetType.ETF:
            return self._fetch_etf(ak, symbol, start, end, with_adj_factor=with_adj_factor)
        if t is AssetType.INDEX:
            return self._fetch_index(ak, symbol, start, end)
        if t is AssetType.FUTURES:
            return self._fetch_sina_contract(ak, symbol, start, end)
        if t is AssetType.FUTURES_CONTINUOUS:
            return self._fetch_sina_continuous(ak, symbol, start, end)
        raise err(
            ErrorCode.PARAM_INVALID,
            f"无法识别 {symbol.value} 的资产类型",
            hint="用 parse_symbol 检查符号；股票/指数/ETF/期货均支持",
        )

    # ------------------------------------------------------------- 股票（EM→新浪）
    def _fetch_stock(
        self,
        ak: Any,
        symbol: Symbol,
        start: pd.Timestamp | None,
        end: pd.Timestamp | None,
        *,
        with_adj_factor: bool,
    ) -> pd.DataFrame:
        try:
            return self._fetch_em(ak, symbol, start, end, with_adj_factor=with_adj_factor, endpoint=ak.stock_zh_a_hist)
        except SolidRockError as em_exc:
            return _fallback_to_sina(
                symbol,
                em_exc,
                lambda: self._fetch_stock_sina(ak, symbol, start, end, with_adj_factor=with_adj_factor),
            )

    def _fetch_stock_sina(
        self,
        ak: Any,
        symbol: Symbol,
        start: pd.Timestamp | None,
        end: pd.Timestamp | None,
        *,
        with_adj_factor: bool,
    ) -> pd.DataFrame:
        """新浪个股日线；pre_close 与复权因子需要前置窗口，故 start 向前多取若干天。"""
        padded = start - pd.Timedelta(days=_SINA_PAD_DAYS) if start is not None else None
        params: dict[str, Any] = {
            "symbol": _sina_a_code(symbol),
            "start_date": _fmt_yyyymmdd(padded, default="19900101"),
            "end_date": _fmt_yyyymmdd(end, default="20991231"),
            "adjust": "",
        }
        raw = self._request(f"新浪日线 {symbol.value}", ak.stock_zh_a_daily, **params)
        if raw is None or len(raw) == 0:
            return pd.DataFrame(columns=list(DAILY_BAR_COLUMN_NAMES))
        hfq = None
        if with_adj_factor:
            hfq = self._request(f"新浪后复权日线 {symbol.value}", ak.stock_zh_a_daily, **dict(params, adjust="hfq"))
        df = _standardize_sina_stock(raw, hfq, symbol)
        return _slice_range(df, start, end)

    # --------------------------------------------------------------- ETF（EM→新浪）
    def _fetch_etf(
        self,
        ak: Any,
        symbol: Symbol,
        start: pd.Timestamp | None,
        end: pd.Timestamp | None,
        *,
        with_adj_factor: bool,
    ) -> pd.DataFrame:
        try:
            return self._fetch_em(ak, symbol, start, end, with_adj_factor=with_adj_factor, endpoint=ak.fund_etf_hist_em)
        except SolidRockError as em_exc:
            return _fallback_to_sina(symbol, em_exc, lambda: self._fetch_etf_sina(ak, symbol, start, end))

    def _fetch_etf_sina(
        self, ak: Any, symbol: Symbol, start: pd.Timestamp | None, end: pd.Timestamp | None
    ) -> pd.DataFrame:
        raw = self._request(f"新浪ETF日线 {symbol.value}", ak.fund_etf_hist_sina, symbol=_sina_a_code(symbol))
        if raw is None or len(raw) == 0:
            return pd.DataFrame(columns=list(DAILY_BAR_COLUMN_NAMES))
        df = _standardize_sina_fund(raw, symbol)
        return _slice_range(df, start, end)

    # -------------------------------------------------------------- 指数（EM→新浪）
    def _fetch_index(
        self, ak: Any, symbol: Symbol, start: pd.Timestamp | None, end: pd.Timestamp | None
    ) -> pd.DataFrame:
        try:
            return self._fetch_em(
                ak,
                symbol,
                start,
                end,
                with_adj_factor=False,
                endpoint=ak.index_zh_a_hist,
                default_factor=1.0,  # 指数无复权概念
                supports_adjust=False,  # index_zh_a_hist 无 adjust 参数
            )
        except SolidRockError as em_exc:
            return _fallback_to_sina(symbol, em_exc, lambda: self._fetch_index_sina(ak, symbol, start, end))

    def _fetch_index_sina(
        self, ak: Any, symbol: Symbol, start: pd.Timestamp | None, end: pd.Timestamp | None
    ) -> pd.DataFrame:
        raw = self._request(f"新浪指数日线 {symbol.value}", ak.stock_zh_index_daily, symbol=_sina_a_code(symbol))
        if raw is None or len(raw) == 0:
            return pd.DataFrame(columns=list(DAILY_BAR_COLUMN_NAMES))
        df = _standardize_sina_index(raw, symbol)
        return _slice_range(df, start, end)

    # ------------------------------------------------------------ 期货（新浪单通道）
    def _fetch_sina_contract(
        self, ak: Any, symbol: Symbol, start: pd.Timestamp | None, end: pd.Timestamp | None
    ) -> pd.DataFrame:
        """新浪期货合约日线（接口不支持日期参数，取全量后本地过滤）。"""
        raw = self._request(f"期货日线 {symbol.value}", ak.futures_zh_daily_sina, symbol=symbol.code)
        if raw is None or len(raw) == 0:
            return pd.DataFrame(columns=list(DAILY_BAR_COLUMN_NAMES))
        df = _standardize_sina_fut(raw, symbol)
        return _slice_range(df, start, end)

    def _fetch_sina_continuous(
        self, ak: Any, symbol: Symbol, start: pd.Timestamp | None, end: pd.Timestamp | None
    ) -> pd.DataFrame:
        """新浪主连日线（新浪主连代码 = 品种 + "0"，如 RB0）。"""
        raw = self._request(
            f"主连日线 {symbol.value}",
            ak.futures_main_sina,
            symbol=to_source_code(symbol, "akshare_sina"),
        )
        if raw is None or len(raw) == 0:
            return pd.DataFrame(columns=list(DAILY_BAR_COLUMN_NAMES))
        df = _standardize_sina_fut(raw, symbol)
        return _slice_range(df, start, end)

    # -------------------------------------------------------------- 其他数据
    def _fetch_calendar(self, start: pd.Timestamp | None, end: pd.Timestamp | None) -> pd.DataFrame:
        ak = _ensure_ak()
        raw = self._request("交易日历", ak.tool_trade_date_hist_sina)
        if raw is None or len(raw) == 0:
            return pd.DataFrame(columns=["date"])
        dates = pd.to_datetime(raw["trade_date"]).dt.normalize()
        df = pd.DataFrame({"date": dates}).drop_duplicates().sort_values("date").reset_index(drop=True)
        return _slice_range(df, start, end)

    def _fetch_instruments(self, asset_type: AssetType) -> pd.DataFrame:
        if asset_type is not AssetType.STOCK:
            raise err(
                ErrorCode.PARAM_INVALID,
                f"akshare 适配器 instruments 暂只支持股票，收到 {asset_type.value}",
                hint="指数/ETF/期货标的列表将在后续版本补充",
            )
        ak = _ensure_ak()
        raw = self._request("股票列表", ak.stock_info_a_code_name)
        if raw is None or len(raw) == 0:
            return pd.DataFrame(columns=["symbol", "name"])
        out = pd.DataFrame(
            {
                "symbol": [f"{str(code).zfill(6)}.{_stock_exchange(str(code).zfill(6))}" for code in raw["code"]],
                "name": raw["name"].astype(str),
            }
        )
        return out.sort_values("symbol").reset_index(drop=True)

    def _fetch_index_constituents(self, index: str) -> pd.DataFrame:
        ak = _ensure_ak()
        idx = parse_symbol(index)
        if idx.asset_type is not AssetType.INDEX:
            raise err(
                ErrorCode.PARAM_INVALID,
                f"{idx.value} 不是指数符号",
                hint="指数形如 000300.SH / 000905.SH / 399006.SZ",
            )
        raw = self._request(f"指数成分 {idx.value}", ak.index_stock_cons_csindex, symbol=idx.code)
        if raw is None or len(raw) == 0:
            return pd.DataFrame(columns=["symbol", "name"])
        out = pd.DataFrame(
            {
                "symbol": [f"{str(code).zfill(6)}.{_stock_exchange(str(code).zfill(6))}" for code in raw["成分券代码"]],
                "name": raw["成分券名称"].astype(str),
            }
        )
        return out.drop_duplicates("symbol").sort_values("symbol").reset_index(drop=True)

    # ------------------------------------------------------ 东方财富主通道
    def _fetch_em(
        self,
        ak: Any,
        symbol: Symbol,
        start: pd.Timestamp | None,
        end: pd.Timestamp | None,
        *,
        endpoint: Any,
        with_adj_factor: bool,
        default_factor: float | None = None,
        supports_adjust: bool = True,
    ) -> pd.DataFrame:
        """东方财富系日线（股票/ETF/指数共用），列名为中文.

        ``supports_adjust``：指数接口没有 ``adjust`` 参数，为 False 时不传。
        """
        params: dict[str, Any] = {
            "symbol": to_source_code(symbol, "akshare_em"),
            "period": "daily",
            "start_date": _fmt_yyyymmdd(start, default="19900101"),
            "end_date": _fmt_yyyymmdd(end, default="20991231"),
        }
        needs_hfq = (
            supports_adjust
            and with_adj_factor
            and symbol.asset_type
            in (
                AssetType.STOCK,
                AssetType.ETF,
            )
        )
        call = dict(params, adjust="") if supports_adjust else params
        raw = self._request(f"日线 {symbol.value}", endpoint, **call)
        if raw is None or len(raw) == 0:
            return pd.DataFrame(columns=list(DAILY_BAR_COLUMN_NAMES))
        hfq = None
        if needs_hfq:
            hfq = self._request(f"后复权日线 {symbol.value}", endpoint, **dict(params, adjust="hfq"))
        df = _standardize_em(raw, hfq, symbol, default_factor=default_factor)
        return _slice_range(df, start, end)


# ---------------------------------------------------------------------- 工具
def _stock_exchange(code: str) -> str:
    """按代码前缀推断 A 股交易所后缀。"""
    if code.startswith("6"):
        return "SH"
    if code.startswith(("4", "8", "9")):
        return "BJ"
    return "SZ"


def _sina_a_code(symbol: Symbol) -> str:
    """统一符号 → 新浪 A 股代码（sz000001 / sh600519）。北交所不受支持。"""
    if symbol.exchange == "BJ":
        raise err(
            ErrorCode.SOURCE_REQUEST_FAILED,
            f"新浪数据源不支持北交所标的 {symbol.value}",
            hint="北交所数据仅东财通道；若东财不可达请稍后重试",
        )
    prefix = "sh" if symbol.exchange == "SH" else "sz"
    return f"{prefix}{symbol.code}"


def _fallback_to_sina(symbol: Symbol, em_exc: SolidRockError, fetcher) -> pd.DataFrame:
    """东财失败后的新浪回退；两条通道都失败时合并报错（保留底层诊断信息）。"""
    try:
        return fetcher()
    except SolidRockError as sina_exc:
        raise err(
            ErrorCode.SOURCE_REQUEST_FAILED,
            f"东财与新浪两条通道均失败：{symbol.value}",
            hint="检查网络连接后重试；若持续失败可尝试升级 akshare（pip install -U akshare）",
            details={
                "eastmoney": em_exc.message,
                "eastmoney_error": em_exc.details.get("error"),
                "sina": sina_exc.message,
                "sina_error": sina_exc.details.get("error"),
            },
        ) from sina_exc


def _fmt_yyyymmdd(ts: pd.Timestamp | None, *, default: str) -> str:
    return ts.strftime("%Y%m%d") if ts is not None else default


def _standardize_em(
    raw: pd.DataFrame,
    hfq: pd.DataFrame | None,
    symbol: Symbol,
    *,
    default_factor: float | None = None,
) -> pd.DataFrame:
    """东财系原始数据 → 标准列；adj_factor 由后复权收盘推得（无法提供时为 NaN）。"""
    df = raw.rename(columns=_EM_RENAME)
    for col in ("open", "high", "low", "close", "volume", "amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    if "turnover_rate" in df.columns:
        df["turnover_rate"] = pd.to_numeric(df["turnover_rate"], errors="coerce")
    # pre_close：东财提供涨跌额（除权后口径），缺失行用 shift 兜底
    if "change" in df.columns:
        df["pre_close"] = pd.to_numeric(df["close"]) - pd.to_numeric(df["change"], errors="coerce")
    else:
        df["pre_close"] = df["close"].shift(1)
    df["adj_factor"] = default_factor if default_factor is not None else np.nan
    if hfq is not None:
        h = hfq.rename(columns=_EM_RENAME)
        h["date"] = pd.to_datetime(h["date"]).dt.normalize()
        raw_close = df.set_index("date")["close"]
        hfq_close = h.set_index("date")["close"].reindex(raw_close.index)
        factor = (hfq_close / raw_close).where(raw_close > 0)
        df["adj_factor"] = factor.ffill().bfill().to_numpy()
    return _finalize(df, symbol)


def _standardize_sina_stock(raw: pd.DataFrame, hfq: pd.DataFrame | None, symbol: Symbol) -> pd.DataFrame:
    """新浪个股 → 标准列.

    单位：volume（股）→ 手；turnover（小数）→ %。
    pre_close 用因子比值法还原除权昨收（与交易所口径一致）：
    ``pre_close_t = close_{t-1} × factor_t / factor_{t-1}``；无因子时退化为 shift(1)。
    """
    df = raw.rename(columns={"turnover": "turnover_rate"}).copy()
    for col in ("open", "high", "low", "close", "volume", "amount", "turnover_rate"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["volume"] = df["volume"] / 100.0  # 股 → 手
    if "turnover_rate" in df.columns:
        df["turnover_rate"] = df["turnover_rate"] * 100.0  # 小数 → %
    df["adj_factor"] = np.nan
    factor: pd.Series | None = None
    if hfq is not None:
        h = hfq.copy()
        h["date"] = pd.to_datetime(h["date"]).dt.normalize()
        raw_close = df.set_index("date")["close"]
        hfq_close = h.set_index("date")["close"].reindex(raw_close.index)
        factor = (hfq_close / raw_close).where(raw_close > 0)
        df["adj_factor"] = factor.ffill().bfill().to_numpy()
    if factor is not None:
        ratio = (factor / factor.shift(1)).reindex(df["date"]).to_numpy()
        df["pre_close"] = df["close"].shift(1) * ratio
    else:
        df["pre_close"] = df["close"].shift(1)
    return _finalize(df, symbol)


def _standardize_sina_fund(raw: pd.DataFrame, symbol: Symbol) -> pd.DataFrame:
    """新浪 ETF → 标准列；无后复权数据，因子为 NaN，pre_close 用 shift(1)。"""
    df = raw.copy()
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["volume"] = df["volume"] / 100.0  # 股 → 手
    df["pre_close"] = df["close"].shift(1)
    df["adj_factor"] = np.nan
    return _finalize(df, symbol)


def _standardize_sina_index(raw: pd.DataFrame, symbol: Symbol) -> pd.DataFrame:
    """新浪指数 → 标准列；因子恒为 1.0（volume 为源原始单位，仅作参考）。"""
    df = raw.copy()
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["pre_close"] = df["close"].shift(1)
    df["adj_factor"] = 1.0
    return _finalize(df, symbol)


def _standardize_sina_fut(raw: pd.DataFrame, symbol: Symbol) -> pd.DataFrame:
    """新浪期货（合约/主连）→ 标准列；无成交额与复权概念。"""
    df = raw.rename(columns=_SINA_FUT_RENAME)
    for col in ("open", "high", "low", "close", "volume", "settle", "open_interest"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["pre_close"] = df["close"].shift(1)
    df["adj_factor"] = 1.0
    return _finalize(df, symbol)


def _finalize(df: pd.DataFrame, symbol: Symbol) -> pd.DataFrame:
    df["symbol"] = symbol.value
    df["suspended"] = False
    for col in DAILY_BAR_COLUMN_NAMES:
        if col not in df.columns:
            df[col] = pd.NA
    return df


def _slice_range(df: pd.DataFrame, start: pd.Timestamp | None, end: pd.Timestamp | None) -> pd.DataFrame:
    if df.empty:
        return df
    mask = pd.Series(True, index=df.index)
    if start is not None:
        mask &= df["date"] >= start
    if end is not None:
        mask &= df["date"] <= end
    return df.loc[mask].reset_index(drop=True)
