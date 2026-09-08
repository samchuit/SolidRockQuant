"""策略文件加载与参数解析（CLI 与 MCP 工具共用）."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from solidrock.strategy.base import Strategy
from solidrock.utils.loader import load_class_from_file

__all__ = ["coerce_param_value", "load_strategy_class", "parse_param_pairs"]


def load_strategy_class(path: str | Path) -> type[Strategy]:
    """加载策略文件中的 Strategy 子类（文件内应只定义一个）.

    供 ``srq backtest run`` 与 MCP ``run_backtest`` 共用。
    """
    return load_class_from_file(path, Strategy, "策略")


def coerce_param_value(raw: str) -> Any:
    """CLI/JSON 之外的字符串参数值 → int/float/bool/str。"""
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def parse_param_pairs(pairs: list[str]) -> dict[str, Any]:
    """解析 ``k=v`` 形式的参数对（CLI 用）。"""
    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            from solidrock.agent.errors import ErrorCode, err

            raise err(
                ErrorCode.PARAM_INVALID,
                f"参数格式错误：{pair!r}",
                hint="应为 k=v 形式，如 --param fast=10",
            )
        key, value = pair.split("=", 1)
        out[key.strip()] = coerce_param_value(value.strip())
    return out
