"""Baostock 适配器（免费，需 login；证券代码格式 ``sz.000001`` / ``sh.600519``）.

覆盖：A股日线（不复权 + 后复权两次取数推导复权因子）、5 分钟线、交易日历、
沪深300/上证50/中证500 成分股。**不支持**：北交所、1 分钟线、期货、ETF/指数日线
（baostock 未提供或本适配器未接）。

单位换算（源 → 标准schema）：
- ``volume``（**股**）÷100 → 手；``turn``（%）原样；``preclose`` 为除权后口径，
  直接映射 pre_close；
- 复权因子：``adjustflag="3"``（原始）与 ``adjustflag="1"``（后复权）各取一次
  收盘价，``adj_factor = hfq_close / close``——与全库"后复权 = 原始 × 因子"
  口径一致，且不依赖 baostock 因子列的基准约定。
"""

from __future__ import annotations

import importlib.util
from typing import Any

import numpy as np
import pandas as pd

from solidrock.agent.errors import ErrorCode, err
from solidrock.data.schema import DAILY_BAR_COLUMN_NAMES
from solidrock.data.sources.base import Capability, DataSource
from solidrock.data.sources.registry import register_source
from solidrock.data.symbols import AssetType, Symbol

# 指数符号 → baostock 成分查询方法名
_INDEX_CONS_METHODS = {
    "000300.SH": "query_hs300_stocks",
    "000016.SH": "query_sz50_stocks",
    "000905.SH": "query_zz500_stocks",
}


def _ensure_bs() -> Any:
    if importlib.util.find_spec("baostock") is None:
        raise err(
            ErrorCode.SOURCE_UNAVAILABLE,
            "数据源 baostock 的依赖库未安装",
            hint="pip install 'solidrock-quant[sources]' 或 pip install baostock",
        )
    import baostock as bs

    return bs


def _baostock_code(symbol: Symbol) -> str:
    """统一符号 → baostock 代码（sz.000001 / sh.600519）。北交所不支持。"""
    if symbol.exchange == "BJ":
        raise err(
            ErrorCode.PARAM_INVALID,
            f"baostock 不支持北交所标的 {symbol.value}",
            hint="北交所请使用 akshare 数据源",
        )
    return f"{symbol.exchange.lower()}.{symbol.code}"


@register_source
class BaostockSource(DataSource):
    name = "baostock"
    capabilities = frozenset(
        {
            Capability.BARS_DAILY_STOCK,
            Capability.BARS_MINUTE_5,
            Capability.CALENDAR,
            Capability.INDEX_CONSTITUENTS,
        }
    )

    def __init__(self) -> None:
        self._bs: Any = None

    @classmethod
    def is_available(cls) -> bool:
        return importlib.util.find_spec("baostock") is not None

    def _api(self) -> Any:
        """惰性登录（baostock 要求模块级 login 后才能查询）。"""
        if self._bs is None:
            bs = _ensure_bs()
            result = self._request("login", bs.login)
            error_code = getattr(result, "error_code", "0")
            if str(error_code) != "0":
                raise err(
                    ErrorCode.SOURCE_AUTH_FAILED,
                    "baostock 登录失败",
                    hint="检查网络；baostock 免费但需要联网登录",
                    details={"error_code": str(error_code)},
                )
            self._bs = bs
        return self._bs

    def _query(self, desc: str, fn, /, **kwargs) -> list[list[str]]:
        """执行查询并收集全部行（baostock 返回游标式结果集）。"""
        rs = self._request(desc, fn, **kwargs)
        error_code = str(getattr(rs, "error_code", "0"))
        if error_code != "0":
            raise err(
                ErrorCode.SOURCE_REQUEST_FAILED,
                f"数据源 baostock 请求失败：{desc}",
                hint="检查参数与网络；baostock 接口偶发限流，稍后重试",
                details={"error_code": error_code},
            )
        rows: list[list[str]] = []
        while getattr(rs, "next", lambda: False)():
            rows.append(rs.get_row_data())
        return rows

    # ------------------------------------------------------------------ 日线
    def _fetch_bars_one(
        self,
        symbol: Symbol,
        start: pd.Timestamp | None,
        end: pd.Timestamp | None,
        *,
        with_adj_factor: bool,
    ) -> pd.DataFrame:
        bs = self._api()
        code = _baostock_code(symbol)
        startdate = start.strftime("%Y-%m-%d") if start is not None else "1990-01-01"
        enddate = end.strftime("%Y-%m-%d") if end is not None else "2099-12-31"
        fields = "date,open,high,low,close,preclose,volume,amount,turn"
        raw_rows = self._query(
            f"日线 {symbol.value}",
            bs.query_history_k_data_plus,
            code=code,
            fields=fields,
            startdate=startdate,
            enddate=enddate,
            frequency="d",
            adjustflag="3",  # 不复权
        )
        if not raw_rows:
            return pd.DataFrame(columns=list(DAILY_BAR_COLUMN_NAMES))
        columns = fields.split(",")
        raw = pd.DataFrame(raw_rows, columns=columns)
        raw["volume"] = pd.to_numeric(raw["volume"], errors="coerce") / 100.0  # 股 → 手
        raw["turn"] = pd.to_numeric(raw["turn"], errors="coerce")
        for col in ("open", "high", "low", "close", "preclose", "amount"):
            raw[col] = pd.to_numeric(raw[col], errors="coerce")
        df = raw.rename(columns={"preclose": "pre_close", "turn": "turnover_rate"})
        df["date"] = pd.to_datetime(df["date"]).dt.normalize()

        df["adj_factor"] = np.nan
        if with_adj_factor:
            hfq_rows = self._query(
                f"后复权日线 {symbol.value}",
                bs.query_history_k_data_plus,
                code=code,
                fields="date,close",
                startdate=startdate,
                enddate=enddate,
                frequency="d",
                adjustflag="1",  # 后复权
            )
            if hfq_rows:
                hfq = pd.DataFrame(hfq_rows, columns=["date", "hfq_close"])
                hfq["date"] = pd.to_datetime(hfq["date"]).dt.normalize()
                hfq["hfq_close"] = pd.to_numeric(hfq["hfq_close"], errors="coerce")
                base = df.set_index("date")["close"]
                factor = (hfq.set_index("date")["hfq_close"] / base).where(base > 0)
                df["adj_factor"] = factor.reindex(df["date"].to_numpy()).ffill().bfill().to_numpy()
        return _finalize(df, symbol)

    # -------------------------------------------------------------- 分钟线
    def _fetch_minutes_one(
        self,
        symbol: Symbol,
        freq: str,
        start: pd.Timestamp | None,
        end: pd.Timestamp | None,
    ) -> pd.DataFrame:
        if freq != "5m":
            raise err(
                ErrorCode.PARAM_INVALID,
                f"baostock 仅支持 5 分钟线，收到 {freq!r}",
                hint="1 分钟线请使用 akshare/tushare 数据源",
            )
        if symbol.asset_type is not AssetType.STOCK:
            raise err(
                ErrorCode.PARAM_INVALID,
                f"baostock 分钟线仅支持股票，收到 {symbol.value}",
            )
        bs = self._api()
        code = _baostock_code(symbol)
        startdate = start.strftime("%Y-%m-%d %H:%M:%S") if start is not None else "1990-01-01 09:00:00"
        enddate = end.strftime("%Y-%m-%d %H:%M:%S") if end is not None else "2099-01-01 15:00:00"
        rows = self._query(
            f"5分钟线 {symbol.value}",
            bs.query_history_k_data_plus,
            code=code,
            fields="time,open,high,low,close,volume",
            startdate=startdate,
            enddate=enddate,
            frequency="5",
            adjustflag="3",
        )
        if not rows:
            return pd.DataFrame(columns=list(DAILY_BAR_COLUMN_NAMES))
        df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["volume"] = df["volume"] / 100.0  # 股 → 手
        # time 形如 20240102093500000（毫秒在后）
        df["date"] = pd.to_datetime(df["time"].str[:14], format="%Y%m%d%H%M%S")
        df["pre_close"] = df["close"].shift(1)
        df["adj_factor"] = np.nan
        return _finalize(df, symbol)

    # -------------------------------------------------------------- 其他数据
    def _fetch_calendar(self, start: pd.Timestamp | None, end: pd.Timestamp | None) -> pd.DataFrame:
        bs = self._api()
        rows = self._query(
            "交易日历",
            bs.query_trade_dates,
            start_date=start.strftime("%Y-%m-%d") if start is not None else "1990-01-01",
            end_date=end.strftime("%Y-%m-%d") if end is not None else "2099-12-31",
        )
        if not rows:
            return pd.DataFrame(columns=["date"])
        df = pd.DataFrame(rows, columns=["calendar_date", "is_trading"])
        dates = pd.to_datetime(df.loc[df["is_trading"] == "1", "calendar_date"]).dt.normalize()
        return pd.DataFrame({"date": dates}).sort_values("date").reset_index(drop=True)

    def _fetch_index_constituents(self, index: str) -> pd.DataFrame:
        bs = self._api()
        from solidrock.data.symbols import parse_symbol

        idx = parse_symbol(index)
        method_name = _INDEX_CONS_METHODS.get(idx.value)
        if method_name is None:
            raise err(
                ErrorCode.PARAM_INVALID,
                f"baostock 成分股仅支持 {sorted(_INDEX_CONS_METHODS)}，收到 {idx.value}",
            )
        rows = self._query(f"成分股 {idx.value}", getattr(bs, method_name))
        if not rows:
            return pd.DataFrame(columns=["symbol", "name"])
        df = pd.DataFrame(rows, columns=["update_date", "code", "name"])
        out = pd.DataFrame(
            {
                "symbol": df["code"].str.replace("sh.", "", regex=False).str.replace("sz.", "", regex=False)
                + "."
                + df["code"].str[:2].str.upper(),
                "name": df["name"],
            }
        )
        return out.drop_duplicates("symbol").sort_values("symbol").reset_index(drop=True)


def _finalize(df: pd.DataFrame, symbol: Symbol) -> pd.DataFrame:
    df["symbol"] = symbol.value
    df["suspended"] = False
    for col in DAILY_BAR_COLUMN_NAMES:
        if col not in df.columns:
            df[col] = pd.NA
    return df
