"""通达信行情数据源（pytdx 直连行情服务器，免费无 token）.

能力与边界（2026-09-10 实测，详见 research/csi1000/tdx_limits_report.txt）：
- 日线：单次 ≤800 根，向前翻页可取**上市以来全量**（如 000001 自 1991-12-23，8247 根）；
- 分钟线：5 分钟约 2 年、1 分钟约 4.5 个月；
- 品种：深/沪主板、创业板（30x/301x）、科创板（688x）、ETF/LOF、债券 ETF、
  沪深指数、可转债均有数据；北交所行情不在标准 hq 协议内（返回空）；
- 除权除息（xdxr）明细为**白盒**数据：适配器用它重建精确的分段复权因子与
  除权后 pre_close，优于第三方源直接下发的黑盒复权序列；
- 频率：单连接实测 23 请求/秒无失败，无显著限速；
- 该服务器池不支持：实时五档批量、历史分笔、财务信息（返回空）。

口径：
- ``volume``：股票/ETF 为**手**（与 pytdx 返回一致）；指数口径未统一（TDX 原样）；
- ``pre_close``：**除权后口径**（分红送转日按 xdxr 公式修正），与交易所昨收基准一致；
- ``adj_factor``：由 xdxr 事件链重建的分段常数（精确），后复权价 = 原始价 × 因子。
"""

from __future__ import annotations

import importlib.util
import threading
import time
from typing import Any

import numpy as np
import pandas as pd

from solidrock.agent.errors import ErrorCode, err
from solidrock.data.schema import DAILY_BAR_COLUMN_NAMES
from solidrock.data.sources.base import Capability, DataSource
from solidrock.data.sources.registry import register_source
from solidrock.data.symbols import Symbol

# 实测可用的行情服务器（K 线/xdxr 均可用），适配器内做故障转移
DEFAULT_SERVERS: list[tuple[str, int]] = [
    ("59.36.5.11", 7709),
    ("117.34.114.13", 7709),
    ("117.34.114.14", 7709),
    ("218.75.126.9", 7709),
]
PAGE = 800  # 服务器单次上限（801 起返回空）
MAX_PAGES = 300  # 单符号翻页上限（= 24 万根，足够任何 A 股全历史）


def _ensure_pytdx() -> Any:
    if importlib.util.find_spec("pytdx") is None:
        raise err(
            ErrorCode.SOURCE_UNAVAILABLE,
            "通达信数据源的依赖库未安装",
            hint="pip install pytdx 或安装扩展：pip install 'solidrock-quant[sources]'",
        )
    from pytdx.hq import TdxHq_API

    return TdxHq_API


@register_source
class TdxSource(DataSource):
    name = "tdx"
    capabilities = frozenset(
        {
            Capability.BARS_DAILY_STOCK,
            Capability.BARS_DAILY_ETF,
            Capability.BARS_DAILY_INDEX,
            Capability.BARS_MINUTE_1,
            Capability.BARS_MINUTE_5,
            Capability.INSTRUMENTS_STOCK,
        }
    )

    def __init__(self, servers: list[tuple[str, int]] | None = None) -> None:
        self.servers = servers or self._load_saved_servers() or DEFAULT_SERVERS
        self._api: Any = None
        self._server_idx = 0
        self._lock = threading.RLock()  # 可重入：_call 持锁期间 _connect 会再次获取
        self._xdxr_cache: dict[str, pd.DataFrame | None] = {}

    @staticmethod
    def _load_saved_servers() -> list[tuple[str, int]]:
        """优先读 srq data tdx-scan 写出的健康服务器列表（内置池殿后兜底）."""
        try:
            import json

            from solidrock.config import get_settings

            f = get_settings().resolved_data_dir() / "tdx_servers.json"
            if not f.exists():
                return []
            data = json.loads(f.read_text(encoding="utf-8"))
            saved = [(str(s[0]), int(s[1])) for s in data.get("servers", [])]
            return saved + [s for s in DEFAULT_SERVERS if s not in saved]
        except Exception:
            return []

    @classmethod
    def is_available(cls) -> bool:
        return importlib.util.find_spec("pytdx") is not None

    # ------------------------------------------------------------------ 连接
    def _connect(self) -> Any:
        """取可用连接；当前连接失效则轮换服务器重连（线程安全）."""
        with self._lock:
            TdxHq_API = _ensure_pytdx()
            if self._api is not None:
                return self._api
            last_exc: Exception | None = None
            for _ in range(len(self.servers)):
                host, port = self.servers[self._server_idx]
                self._server_idx = (self._server_idx + 1) % len(self.servers)
                api = TdxHq_API()
                try:
                    if api.connect(host, port, time_out=6):
                        self._api = api
                        return api
                except Exception as exc:
                    last_exc = exc
            raise err(
                ErrorCode.SOURCE_REQUEST_FAILED,
                "通达信行情服务器全部连接失败",
                hint="服务器池可经 TdxSource(servers=[(ip, port), ...]) 自定义；"
                "pytdx 服务器池见 pytdx.config.hosts.hq_hosts",
                details={"error": str(last_exc) if last_exc else None},
            )

    def _reset(self) -> None:
        with self._lock:
            try:
                if self._api is not None:
                    self._api.disconnect()
            except Exception:
                pass
            self._api = None

    def _call(self, method: str, *args) -> Any:
        """带故障转移的调用：失败重连下一台服务器后重试一次.

        单一 socket 连接不支持并发请求，整个调用过程持锁串行化
        （多线程共用 TdxSource 时不会穿插请求；23 请求/秒的单连接吞吐足够批量拉取）。
        """
        for attempt in range(2):
            with self._lock:
                api = self._connect()
                try:
                    return getattr(api, method)(*args)
                except Exception:
                    self._reset()
                    if attempt == 1:
                        raise
            time.sleep(0.2)
        return None

    # ------------------------------------------------------------------ 日线
    def _market(self, symbol: Symbol) -> int | None:
        if symbol.exchange == "SH":
            return 1
        if symbol.exchange == "SZ":
            return 0
        return None  # 北交所等不在标准 hq 协议内

    def _fetch_bars_one(
        self,
        symbol: Symbol,
        start: pd.Timestamp | None,
        end: pd.Timestamp | None,
        *,
        with_adj_factor: bool,
    ) -> pd.DataFrame:
        market = self._market(symbol)
        if market is None:
            return pd.DataFrame(columns=list(DAILY_BAR_COLUMN_NAMES))
        # 指数必须走 get_index_bars（get_security_bars 对指数的 datetime 解析错位）
        is_index = symbol.asset_type is not None and symbol.asset_type.value == "index"
        bars_method = "get_index_bars" if is_index else "get_security_bars"
        pages: list[list[dict]] = []
        offset = 0
        first_dt: str | None = None
        for _ in range(MAX_PAGES):
            bars = self._call(bars_method, 9, market, symbol.code, offset, PAGE)
            if not bars:
                break
            pages.insert(0, bars)
            first_dt = bars[0]["datetime"][:10]
            offset += PAGE
            if start is not None and first_dt <= start.strftime("%Y-%m-%d"):
                break
        if not pages:
            return pd.DataFrame(columns=list(DAILY_BAR_COLUMN_NAMES))
        flat = [row for page in pages for row in page]
        df = pd.DataFrame(flat)
        df["date"] = pd.to_datetime(df["datetime"]).dt.normalize()
        df = df.drop_duplicates("datetime").sort_values("date").reset_index(drop=True)
        df = df.rename(columns={"vol": "volume"})
        if start is not None:
            df = df[df["date"] >= start]
        if end is not None:
            df = df[df["date"] <= end]
        if df.empty:
            return pd.DataFrame(columns=list(DAILY_BAR_COLUMN_NAMES))

        out = pd.DataFrame(
            {
                "symbol": symbol.value,
                "date": df["date"].to_numpy(),
                "open": df["open"].astype(float).to_numpy(),
                "high": df["high"].astype(float).to_numpy(),
                "low": df["low"].astype(float).to_numpy(),
                "close": df["close"].astype(float).to_numpy(),
                "volume": df["volume"].astype(float).to_numpy(),
                "amount": df["amount"].astype(float).to_numpy(),
            }
        )
        # pre_close 初值 = 昨日收盘；除权后口径与复权因子由 xdxr 修正
        out["pre_close"] = out["close"].shift(1)
        out["turnover_rate"] = np.nan
        out["suspended"] = False
        out["adj_factor"] = np.nan
        if with_adj_factor:
            out = self._apply_xdxr(out, symbol)
        else:
            out["adj_factor"] = 1.0
        return out.reindex(columns=list(DAILY_BAR_COLUMN_NAMES))

    # ------------------------------------------------------------------ xdxr
    def _get_xdxr(self, symbol: Symbol) -> pd.DataFrame | None:
        """xdxr 明细（按 code 缓存；全服务器失败返回 None 并降级）."""
        key = symbol.code
        if key in self._xdxr_cache:
            return self._xdxr_cache[key]
        market = self._market(symbol)
        if market is None:
            self._xdxr_cache[key] = None
            return None
        try:
            rows = self._call("get_xdxr_info", market, key)
            df = pd.DataFrame(rows) if rows else pd.DataFrame()
        except Exception:
            df = pd.DataFrame()
        self._xdxr_cache[key] = df if not df.empty else None
        return self._xdxr_cache[key]

    def _apply_xdxr(self, out: pd.DataFrame, symbol: Symbol) -> pd.DataFrame:
        """xdxr 事件 → 分段复权因子 + 除权后 pre_close（精确，白盒）.

        事件口径（pytdx get_xdxr_info）：
        - category 1 除权除息：d=fenhong/10 元/股，s=songzhuangu/10 送股比例，
          p=peigu/10 配股比例（价 peigujia）；
          除权参考价 = (prev_close - d + p*pg)/(1+s+p)，因子比 = prev_close/除权参考价；
        - category 11 扩缩股：因子比 = suogu；
        - 其余类别（股本变化等）无价格调整。
        """
        df = self._get_xdxr(symbol)
        factor = np.ones(len(out))
        pre_close = out["pre_close"].to_numpy().copy()
        if df is None or df.empty:
            out["adj_factor"] = factor
            return out
        # 事件表
        evs: list[tuple[pd.Timestamp, float]] = []
        raw_date_to_prev_close = {
            out["date"].iloc[i]: (out["close"].iloc[i - 1] if i > 0 else np.nan) for i in range(len(out))
        }
        date_index = pd.DatetimeIndex(out["date"])
        for _, r in df.iterrows():
            try:
                d = pd.Timestamp(year=int(r["year"]), month=int(r["month"]), day=int(r["day"]))
                cat = int(r["category"])
            except Exception:
                continue
            if d not in date_index:
                continue
            i = date_index.get_loc(d)
            prev_close = raw_date_to_prev_close.get(d, np.nan)
            if not np.isfinite(prev_close) or prev_close <= 0:
                continue
            ratio: float | None = None
            if cat == 1:
                d_cash = float(r["fenhong"]) / 10.0 if pd.notna(r["fenhong"]) else 0.0
                s = float(r["songzhuangu"]) / 10.0 if pd.notna(r["songzhuangu"]) else 0.0
                p = float(r["peigu"]) / 10.0 if pd.notna(r["peigu"]) else 0.0
                pg = float(r["peigujia"]) if pd.notna(r["peigujia"]) else 0.0
                if d_cash == 0 and s == 0 and p == 0:
                    continue
                ref_price = (prev_close - d_cash + p * pg) / (1 + s + p)
                if ref_price <= 0:
                    continue
                ratio = prev_close / ref_price
                pre_close[i] = ref_price  # 除权后口径昨收
            elif cat == 11 and pd.notna(r["suogu"]):
                ratio = float(r["suogu"])
                pre_close[i] = prev_close * ratio
            if ratio is None or not np.isfinite(ratio) or not 0.02 < ratio < 25:
                continue
            evs.append((d, ratio))
        # 因子路径（分段常数，事件日生效）
        cur = 1.0
        ev_map = dict(evs)
        for i in range(len(out)):
            ratio = ev_map.get(date_index[i])
            if ratio is not None:
                cur *= ratio
            factor[i] = cur
        out["adj_factor"] = factor
        out["pre_close"] = pre_close
        return out

    # ------------------------------------------------------------------ 分钟线
    def _fetch_minutes_one(
        self,
        symbol: Symbol,
        freq: str,
        start: pd.Timestamp | None,
        end: pd.Timestamp | None,
    ) -> pd.DataFrame:
        market = self._market(symbol)
        if market is None:
            return pd.DataFrame(columns=list(DAILY_BAR_COLUMN_NAMES))
        category = 0 if freq == "5m" else 8  # 0=5分钟, 8=1分钟
        pages: list[list[dict]] = []
        offset = 0
        first_dt: str | None = None
        for _ in range(MAX_PAGES):
            bars = self._call("get_security_bars", category, market, symbol.code, offset, PAGE)
            if not bars:
                break
            pages.insert(0, bars)
            first_dt = bars[0]["datetime"][:10]
            offset += PAGE
            if start is not None and first_dt <= start.strftime("%Y-%m-%d"):
                break
        if not pages:
            return pd.DataFrame(columns=list(DAILY_BAR_COLUMN_NAMES))
        flat = [row for page in pages for row in page]
        df = pd.DataFrame(flat).drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)
        df["date"] = pd.to_datetime(df["datetime"])
        if start is not None:
            df = df[df["date"] >= start]
        if end is not None:
            df = df[df["date"] <= end + pd.Timedelta(days=1)]
        if df.empty:
            return pd.DataFrame(columns=list(DAILY_BAR_COLUMN_NAMES))
        out = pd.DataFrame(
            {
                "symbol": symbol.value,
                "date": df["date"].to_numpy(),
                "open": df["open"].astype(float).to_numpy(),
                "high": df["high"].astype(float).to_numpy(),
                "low": df["low"].astype(float).to_numpy(),
                "close": df["close"].astype(float).to_numpy(),
                "volume": df["vol"].astype(float).to_numpy(),
                "amount": df["amount"].astype(float).to_numpy(),
                "pre_close": np.nan,
                "turnover_rate": np.nan,
                "adj_factor": np.nan,  # 分钟数据无复权（与 akshare 适配器约定一致）
                "suspended": False,
            }
        )
        return out.reindex(columns=list(DAILY_BAR_COLUMN_NAMES))

    # ------------------------------------------------------------------ 标的列表
    def _fetch_instruments(self, asset_type: Any = None) -> pd.DataFrame:
        rows: list[dict] = []
        for market, exchange in ((0, "SZ"), (1, "SH")):
            offset = 0
            while True:
                lst = self._call("get_security_list", market, offset)
                if not lst:
                    break
                for it in lst:
                    rows.append(
                        {
                            "symbol": f"{it['code']}.{exchange}",
                            "name": it.get("name", ""),
                        }
                    )
                offset += len(lst)
                if offset > 30000:
                    break
        return pd.DataFrame(rows)
