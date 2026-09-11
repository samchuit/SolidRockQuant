"""错误体系：稳定错误码 + 修复建议（hint）规范.

全库统一抛出 :class:`SolidRockError`。每条错误必须携带：

- ``code``：稳定的枚举错误码，Agent 依赖它做分支判断；只能追加，不能改名或删除；
- ``message``：人读描述；
- ``hint``：**可执行**的修复建议，Agent 依赖它自纠错，这是"Agent 友好"的硬性要求。

示例::

    raise err(
        ErrorCode.SYMBOL_NOT_FOUND,
        f"代码 {raw} 在数据源中不存在",
        hint="先调用 search_instruments 确认符号；股票代码需带交易所后缀，如 000001.SZ",
    )
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """稳定错误码（对外契约，只追加不修改）。"""

    # --- 请求与符号 ---
    SYMBOL_INVALID = "SYMBOL_INVALID"  # 符号格式非法
    SYMBOL_NOT_FOUND = "SYMBOL_NOT_FOUND"  # 格式合法但数据源查无此标的
    PARAM_INVALID = "PARAM_INVALID"  # 参数非法（日期区间倒置等）

    # --- 数据源 ---
    SOURCE_NOT_REGISTERED = "SOURCE_NOT_REGISTERED"  # 未注册的数据源名
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"  # 依赖库未安装或网络不可达
    SOURCE_AUTH_FAILED = "SOURCE_AUTH_FAILED"  # 鉴权失败（token 缺失/无效/积分不足）
    SOURCE_REQUEST_FAILED = "SOURCE_REQUEST_FAILED"  # 请求失败（限流/超时/接口变动）
    CAPABILITY_NOT_SUPPORTED = "CAPABILITY_NOT_SUPPORTED"  # 数据源不具备该能力

    # --- 本地数据 ---
    NO_DATA = "NO_DATA"  # 本地无数据，需要先更新
    DATA_FORMAT_INVALID = "DATA_FORMAT_INVALID"  # 数据不符合标准 schema
    SNAPSHOT_NOT_FOUND = "SNAPSHOT_NOT_FOUND"
    SNAPSHOT_EXISTS = "SNAPSHOT_EXISTS"
    SNAPSHOT_READ_ONLY = "SNAPSHOT_READ_ONLY"  # 快照是只读副本

    # --- 策略校验（M3+ 回测/Agent 层使用） ---
    STRATEGY_INVALID = "STRATEGY_INVALID"
    LOOKAHEAD_SUSPECTED = "LOOKAHEAD_SUSPECTED"

    # --- 其他 ---
    INTERNAL_ERROR = "INTERNAL_ERROR"  # 未预期的框架错误（应附复现步骤报 issue）
    TIMEOUT = "TIMEOUT"  # 沙箱/任务超时被终止

    # --- 实盘（live，v0.3+） ---
    LIVE_UNAVAILABLE = "LIVE_UNAVAILABLE"  # 实盘通道不可用（cfquant 未安装/QMT 离线）
    LIVE_ORDER_FAILED = "LIVE_ORDER_FAILED"  # 实盘下单/撤单被拒绝
    LIVE_READ_ONLY = "LIVE_READ_ONLY"  # 实盘通道配置为只读，禁止下单
    LIVE_SYMBOL_NOT_ALLOWED = "LIVE_SYMBOL_NOT_ALLOWED"  # 标的不在实盘白名单内
    LIVE_ORDER_TOO_LARGE = "LIVE_ORDER_TOO_LARGE"  # 单笔名义金额超过上限
    LIVE_NOT_TRADING_HOURS = "LIVE_NOT_TRADING_HOURS"  # 非交易日或非交易时段
    LIVE_DUPLICATE_ORDER = "LIVE_DUPLICATE_ORDER"  # 幂等去重命中（疑似重复提交）


class SolidRockError(Exception):
    """框架内所有受检错误的基类。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """输出 MCP 信封中的 ``error`` 段（见 docs/design.md §8.2）。"""
        out: dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        if self.details:
            out["details"] = self.details
        return out

    def __str__(self) -> str:
        text = f"[{self.code.value}] {self.message}"
        if self.hint:
            text += f"\nhint: {self.hint}"
        return text


def err(
    code: ErrorCode,
    message: str,
    *,
    hint: str | None = None,
    details: dict[str, Any] | None = None,
) -> SolidRockError:
    """便捷构造函数。"""
    return SolidRockError(code, message, hint=hint, details=details)
