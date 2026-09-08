"""Tushare Pro 适配器（质量更高，需 token 与积分）.

覆盖：A股日线（daily+adj_factor）、指数日线（index_daily）、ETF 日线
（fund_daily+fund_adj）、期货合约日线（fut_daily）、交易日历（trade_cal）。

单位换算（源 → 标准schema）：
- daily / index_daily / fund_daily：``vol``（手）原样 → volume；
  ``amount``（**千元**）×1000 → 元；
- fut_daily：``vol``（手）原样；``amount`` 官方标注**万元**，×1e4 → 元
  （TODO(unit)：待真实数据核验）；``pre_settle`` 映射为 settle 语义以外的字段，
  pre_close 用 close.shift(1)；``oi`` → open_interest。

Tushare 的 ``adj_factor`` 与我们的后复权口径一致（hfq = close × factor），
直接落库；指数/期货恒为 1.0。
"""

from __future__ import annotations

import importlib.util
import time
from typing import Any

import numpy as np
import pandas as pd

from solidrock.agent.errors import ErrorCode, SolidRockError, err
from solidrock.config import get_settings
from solidrock.data.schema import DAILY_BAR_COLUMN_NAMES
from solidrock.data.sources.base import Capability, DataSource
from solidrock.data.sources.registry import register_source
from solidrock.data.symbols import AssetType, Symbol


def _ensure_tushare() -> Any:
    if importlib.util.find_spec("tushare") is None:
        raise err(
            ErrorCode.SOURCE_UNAVAILABLE,
            "数据源 tushare 的依赖库未安装",
            hint="pip install 'solidrock-quant[sources]' 或 pip install tushare",
        )
    import tushare as ts

    return ts


@register_source
class TushareSource(DataSource):
    name = "tushare"
    capabilities = frozenset(
        {
            Capability.BARS_DAILY_STOCK,
            Capability.BARS_DAILY_INDEX,
            Capability.BARS_DAILY_ETF,
            Capability.BARS_DAILY_FUTURES,
            Capability.CALENDAR,
        }
    )

    def __init__(
        self,
        token: str | None = None,
        request_interval: float = 0.12,
    ) -> None:
        """``token`` 缺省取配置 ``SOLIDROCK_TUSHARE_TOKEN``；
        ``request_interval`` 为每次 API 调用的间隔秒数（Tushare 有 QPS 限制）。"""
        self.token = token or get_settings().tushare_token
        self.request_interval = request_interval
        self._pro: Any = None
        self._call_count = 0

    @classmethod
    def is_available(cls) -> bool:
        return importlib.util.find_spec("tushare") is not None

    def _api(self) -> Any:
        if self._pro is None:
            ts = _ensure_tushare()
            if not self.token:
                raise err(
                    ErrorCode.SOURCE_AUTH_FAILED,
                    "Tushare token 未配置",
                    hint="设置环境变量 SOLIDROCK_TUSHARE_TOKEN=<你的token>"
                    "（在 https://tushare.pro 注册获取），或 TushareSource(token=...)",
                )
            try:
                ts.set_token(self.token)
                self._pro = ts.pro_api()
            except Exception as exc:
                raise err(
                    ErrorCode.SOURCE_AUTH_FAILED,
                    "Tushare 初始化失败",
                    hint="检查 token 是否有效；部分接口需要 2000 积分以上",
                    details={"error": str(exc)},
                ) from exc
        return self._pro

    def _call(self, desc: str, fn, /, **kwargs) -> pd.DataFrame:
        """节流 + 请求 + 积分不足等错误翻译。"""
        self._call_count += 1
        if self.request_interval > 0:
            time.sleep(self.request_interval)
        try:
            df = fn(**kwargs)
        except SolidRockError:
            raise
        except Exception as exc:
            text = str(exc)
            if "积分" in text or "permission" in text.lower() or "40203" in text:
                raise err(
                    ErrorCode.SOURCE_AUTH_FAILED,
                    f"Tushare 接口权限不足：{desc}",
                    hint="该接口需要更高积分；检查账号积分或换用 akshare 数据源",
                    details={"error": text},
                ) from exc
            raise err(
                ErrorCode.SOURCE_REQUEST_FAILED,
                f"数据源 tushare 请求失败：{desc}",
                hint="检查网络与 token；若持续失败可尝试升级 tushare（pip install -U tushare）",
                details={"error": text},
            ) from exc
        return df if df is not None else pd.DataFrame()

    # ------------------------------------------------------------------ 日线
    def _fetch_bars_one(
        self,
        symbol: Symbol,
        start: pd.Timestamp | None,
        end: pd.Timestamp | None,
        *,
        with_adj_factor: bool,
    ) -> pd.DataFrame:
        pro = self._api()
        params = {
            "ts_code": symbol.value,
            "start_date": _fmt(start, default="19900101"),
            "end_date": _fmt(end, default="20991231"),
        }
        t = symbol.asset_type
        if t is AssetType.STOCK:
            raw = self._call(f"daily {symbol.value}", pro.daily, **params)
            factor = None
            if with_adj_factor:
                adj = self._call(f"adj_factor {symbol.value}", pro.adj_factor, **params)
                factor = _factor_series(adj)
            return _standardize_ts(raw, symbol, factor=factor, amount_scale=1_000.0)
        if t is AssetType.INDEX:
            raw = self._call(f"index_daily {symbol.value}", pro.index_daily, **params)
            return _standardize_ts(raw, symbol, factor=None, amount_scale=1_000.0, index_like=True)
        if t is AssetType.ETF:
            raw = self._call(f"fund_daily {symbol.value}", pro.fund_daily, **params)
            factor = None
            if with_adj_factor:
                adj = self._call(f"fund_adj {symbol.value}", pro.fund_adj, **params)
                factor = _factor_series(adj)
            return _standardize_ts(raw, symbol, factor=factor, amount_scale=1_000.0)
        if t is AssetType.FUTURES:
            raw = self._call(f"fut_daily {symbol.value}", pro.fut_daily, **params)
            # TODO(unit)：fut_daily 的 amount 官方标注为万元，待真实数据核验
            return _standardize_ts(raw, symbol, factor=None, amount_scale=10_000.0, futures=True)
        raise err(
            ErrorCode.PARAM_INVALID,
            f"tushare 适配器不支持 {symbol.value}（主连请换用 akshare 数据源）",
            hint="主连形如 RB.SHFE；tushare 主连需合约映射，计划在 v0.2 支持",
        )

    # -------------------------------------------------------------- 其他数据
    def _fetch_calendar(self, start: pd.Timestamp | None, end: pd.Timestamp | None) -> pd.DataFrame:
        pro = self._api()
        raw = self._call(
            "交易日历",
            pro.trade_cal,
            exchange="SSE",
            start_date=_fmt(start, default="19900101"),
            end_date=_fmt(end, default="20991231"),
            is_open="1",
        )
        if raw.empty:
            return pd.DataFrame(columns=["date"])
        dates = pd.to_datetime(raw["cal_date"], format="%Y%m%d").dt.normalize()
        return pd.DataFrame({"date": dates}).drop_duplicates().sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------- 工具
def _fmt(ts: pd.Timestamp | None, *, default: str) -> str:
    return ts.strftime("%Y%m%d") if ts is not None else default


def _factor_series(adj: pd.DataFrame) -> pd.Series | None:
    """把 adj_factor 接口返回转成以 date 为索引的因子序列。"""
    if adj is None or adj.empty:
        return None
    out = adj.copy()
    out["date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d").dt.normalize()
    out["adj_factor"] = pd.to_numeric(out["adj_factor"], errors="coerce")
    return out.dropna(subset=["adj_factor"]).set_index("date")["adj_factor"]


def _standardize_ts(
    raw: pd.DataFrame,
    symbol: Symbol,
    *,
    factor: pd.Series | None,
    amount_scale: float,
    index_like: bool = False,
    futures: bool = False,
) -> pd.DataFrame:
    """Tushare 日线原始数据 → 标准列."""
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=list(DAILY_BAR_COLUMN_NAMES))
    df = raw.rename(columns={"vol": "volume", "oi": "open_interest"})
    df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.normalize()
    # Tushare 返回按日期降序，统一升序再计算
    df = df.sort_values("date", kind="stable").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "amount" in df.columns:
        df["amount"] = df["amount"] * amount_scale
    if futures:
        # fut_daily 只有 pre_settle（昨结算）；pre_close 用 shift(1) 近似
        df["pre_close"] = df["close"].shift(1)
    else:
        df["pre_close"] = pd.to_numeric(df["pre_close"], errors="coerce")
    if factor is not None:
        joined = factor.reindex(pd.DatetimeIndex(df["date"]))
        df["adj_factor"] = joined.ffill().bfill().to_numpy()
    elif index_like or futures:
        df["adj_factor"] = 1.0
    else:
        df["adj_factor"] = np.nan
    if "settle" not in df.columns:
        df["settle"] = pd.NA
    if "open_interest" not in df.columns:
        df["open_interest"] = pd.NA
    df["symbol"] = symbol.value
    df["suspended"] = False
    for col in DAILY_BAR_COLUMN_NAMES:
        if col not in df.columns:
            df[col] = pd.NA
    return df
