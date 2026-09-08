"""数据源注册表：注册 / 创建 / 列举."""

from __future__ import annotations

from typing import Any, TypeVar

from solidrock.agent.errors import ErrorCode, err
from solidrock.data.sources.base import DataSource

_REGISTRY: dict[str, type[DataSource]] = {}

T = TypeVar("T", bound=type[DataSource])


def register_source(cls: T) -> T:
    """类装饰器：注册数据源。重名注册直接报错（避免静默覆盖）。"""
    name = getattr(cls, "name", None)
    if not name:
        raise ValueError(f"{cls.__qualname__} 缺少 name 类属性，无法注册为数据源")
    existing = _REGISTRY.get(name)
    if existing is not None and existing is not cls:
        raise ValueError(f"数据源名 {name!r} 已被 {existing.__module__}.{existing.__qualname__} 注册")
    _REGISTRY[name] = cls
    return cls


def create_source(name: str, **kwargs: Any) -> DataSource:
    """按名称实例化数据源。"""
    cls = _REGISTRY.get(name)
    if cls is None:
        raise err(
            ErrorCode.SOURCE_NOT_REGISTERED,
            f"未注册的数据源 {name!r}",
            hint=f"已注册数据源：{sorted(_REGISTRY)}；"
            "若 akshare/tushare 未出现在列表中，先安装扩展：pip install 'solidrock-quant[sources]'",
        )
    return cls(**kwargs)


def list_sources() -> list[dict[str, Any]]:
    """全部已注册数据源及其能力与可用性（供 Agent 与 CLI 展示）。"""
    return [
        {
            "name": cls.name,
            "capabilities": sorted(c.value for c in cls.capabilities),
            "available": cls.is_available(),
        }
        for cls in _REGISTRY.values()
    ]


def registered_names() -> list[str]:
    return sorted(_REGISTRY)
