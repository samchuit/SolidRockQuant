"""DataSource 抽象基类、能力声明与通用取数流程.

适配器只需实现 ``_fetch_bars_one``（单符号 → 源始数据 → 标准列），
基类负责：符号校验、日期区间规范化、多符号循环、标准 schema 校验。

实现要求（写新适配器前必读）：
- 单位换算在适配器内完成，出适配器的数据必须符合 schema 约定
  （volume=手、amount=元、pre_close=除权后口径）；
- 依赖库一律**方法内惰性导入**，缺库抛 ``SOURCE_UNAVAILABLE``（带安装 hint），
  不允许在模块顶层 import 第三方数据源库；
- 所有对源的网络调用经 ``_request`` 包装，异常统一转为
  ``SOURCE_REQUEST_FAILED``（带可执行 hint）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import TYPE_CHECKING, ClassVar

import pandas as pd

from solidrock.agent.errors import ErrorCode, SolidRockError, err
from solidrock.data.schema import empty_bars_frame, validate_bars
from solidrock.data.symbols import AssetType, Symbol, validate_symbols

if TYPE_CHECKING:
    from collections.abc import Sequence


class Capability(str, Enum):
    """数据源能力（适配器声明，供路由与文档生成）。"""

    BARS_DAILY_STOCK = "bars_daily_stock"
    BARS_DAILY_INDEX = "bars_daily_index"
    BARS_DAILY_ETF = "bars_daily_etf"
    BARS_DAILY_FUTURES = "bars_daily_futures"
    BARS_DAILY_FUTURES_CONTINUOUS = "bars_daily_futures_continuous"
    CALENDAR = "calendar"
    INSTRUMENTS_STOCK = "instruments_stock"
    INDEX_CONSTITUENTS = "index_constituents"

    # 资产类型 → 对应日线能力
    @classmethod
    def bars_for(cls, asset_type: AssetType) -> Capability:
        mapping = {
            AssetType.STOCK: cls.BARS_DAILY_STOCK,
            AssetType.INDEX: cls.BARS_DAILY_INDEX,
            AssetType.ETF: cls.BARS_DAILY_ETF,
            AssetType.FUTURES: cls.BARS_DAILY_FUTURES,
            AssetType.FUTURES_CONTINUOUS: cls.BARS_DAILY_FUTURES_CONTINUOUS,
        }
        if asset_type not in mapping:
            raise err(ErrorCode.PARAM_INVALID, f"未知的资产类型 {asset_type!r}")
        return mapping[asset_type]


class DataSource(ABC):
    """数据源适配器基类。"""

    name: ClassVar[str]
    capabilities: ClassVar[frozenset[Capability]] = frozenset()

    # ------------------------------------------------------------------ 日线
    def fetch_bars(
        self,
        symbols: str | Sequence[str],
        start: str | pd.Timestamp | None = None,
        end: str | pd.Timestamp | None = None,
        freq: str = "1d",
        *,
        with_adj_factor: bool = True,
    ) -> pd.DataFrame:
        """拉取日线并返回标准 schema（多符号逐个请求后合并）.

        返回空 DataFrame 表示区间内无数据；符号在源中不存在时同样返回空
        （无法与"区间无数据"区分时，以空为准，由调用方决定是否报错）。
        """
        if freq != "1d":
            raise err(
                ErrorCode.PARAM_INVALID,
                f"v0.1 仅支持日线 freq='1d'，收到 {freq!r}",
                hint="分钟线计划在 v0.2 支持",
            )
        start_ts, end_ts = normalize_range(start, end)
        symbol_list: list[Symbol] = (
            validate_symbols([symbols]) if isinstance(symbols, str) else validate_symbols(list(symbols))
        )
        frames = [self._fetch_bars_one(sym, start_ts, end_ts, with_adj_factor=with_adj_factor) for sym in symbol_list]
        frames = [f for f in frames if f is not None and not f.empty]
        if not frames:
            return empty_bars_frame()
        return validate_bars(pd.concat(frames, ignore_index=True))

    @abstractmethod
    def _fetch_bars_one(
        self,
        symbol: Symbol,
        start: pd.Timestamp | None,
        end: pd.Timestamp | None,
        *,
        with_adj_factor: bool,
    ) -> pd.DataFrame:
        """拉取单符号日线，返回已映射为标准列名的 DataFrame（可为空）。"""

    # -------------------------------------------------------------- 其他数据
    def fetch_calendar(
        self, start: str | pd.Timestamp | None = None, end: str | pd.Timestamp | None = None
    ) -> pd.DataFrame:
        """交易日历，返回单列 ``date``（datetime64，升序）。"""
        self.require(Capability.CALENDAR)
        return self._fetch_calendar(start, end)

    def fetch_instruments(self, asset_type: AssetType = AssetType.STOCK) -> pd.DataFrame:
        """标的列表，返回列 ``symbol, name``（以及适配器自带的附加列）。"""
        self.require(Capability.INSTRUMENTS_STOCK)
        return self._fetch_instruments(asset_type)

    def fetch_index_constituents(self, index: str) -> pd.DataFrame:
        """指数成分股，返回列 ``symbol, name``。``index`` 形如 ``000300.SH``。"""
        self.require(Capability.INDEX_CONSTITUENTS)
        return self._fetch_index_constituents(index)

    # 以下由有能力声明且实现了对应方法的适配器覆盖
    def _fetch_calendar(self, start: pd.Timestamp | None, end: pd.Timestamp | None) -> pd.DataFrame:
        raise NotImplementedError

    def _fetch_instruments(self, asset_type: AssetType) -> pd.DataFrame:
        raise NotImplementedError

    def _fetch_index_constituents(self, index: str) -> pd.DataFrame:
        raise NotImplementedError

    # ------------------------------------------------------------------ 工具
    def require(self, capability: Capability) -> None:
        """能力不足时抛 ``CAPABILITY_NOT_SUPPORTED``（hint 指出可用能力）。"""
        if capability not in self.capabilities:
            raise err(
                ErrorCode.CAPABILITY_NOT_SUPPORTED,
                f"数据源 {self.name!r} 不支持能力 {capability.value}",
                hint=f"{self.name!r} 当前能力：{sorted(c.value for c in self.capabilities)}；"
                "用 solidrock.data.sources.list_sources() 查看全部数据源",
            )

    @classmethod
    def is_available(cls) -> bool:
        """依赖库是否已安装（子类可覆盖为 importlib.util.find_spec 检查）。"""
        return True

    def _request(self, desc: str, fn, /, **kwargs):
        """统一包装源请求：异常转 ``SOURCE_REQUEST_FAILED`` 并带修复 hint。"""
        try:
            return fn(**kwargs)
        except SolidRockError:
            raise
        except Exception as exc:
            raise err(
                ErrorCode.SOURCE_REQUEST_FAILED,
                f"数据源 {self.name!r} 请求失败：{desc}",
                hint="检查网络连接；若接口报错，尝试升级数据源库（pip install -U akshare tushare）后重试",
                details={"error": str(exc)},
            ) from exc


def normalize_range(
    start: str | pd.Timestamp | None, end: str | pd.Timestamp | None
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """日期区间规范化；start > end 抛 ``PARAM_INVALID``。"""

    def _to_ts(value: str | pd.Timestamp | None) -> pd.Timestamp | None:
        if value is None:
            return None
        ts = pd.Timestamp(value)
        return None if pd.isna(ts) else ts.normalize()

    start_ts, end_ts = _to_ts(start), _to_ts(end)
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise err(
            ErrorCode.PARAM_INVALID,
            f"日期区间倒置：start={start_ts.date()} > end={end_ts.date()}",
            hint="start 应早于或等于 end，格式如 2024-01-01",
        )
    return start_ts, end_ts
