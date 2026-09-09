"""插件机制：第三方包通过 Python entry-points 接入.

插件包在自己的 pyproject 中声明 entry-points，安装后即被框架发现：

```toml
[project.entry-points."solidrock.sources"]
my-source = "my_plugin.source"          # 模块（内含 @register_source 的 DataSource 子类）

[project.entry-points."solidrock.factors"]
my-momentum = "my_plugin.factors:MyMomentum"   # 模块:Factor 子类
```

- ``solidrock.sources``：值为**模块路径**，导入即触发 ``@register_source`` 注册；
- ``solidrock.factors``：值为 ``模块:类名``，加载后注册进因子库（注册名 = entry-point 名）。

发现是惰性的：仅在按名找不到数据源/因子时自动扫描一次，也可显式调用
``discover_plugins()``。加载失败的插件会被跳过并记录（不阻塞其余插件）。
"""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from typing import Any

from solidrock.agent.errors import ErrorCode, err

PLUGIN_GROUPS: dict[str, str] = {
    "sources": "solidrock.sources",
    "factors": "solidrock.factors",
}

_discovered: set[str] = set()


def discover_plugins(group: str | None = None) -> dict[str, list[str]]:
    """扫描并导入全部已安装插件；返回 ``{group: [插件名]}``.

    ``group`` 缺省扫描所有组。重复调用安全（已扫描的组自动跳过，
    除非传入显式 group 强制重扫）。
    """
    groups = [group] if group is not None else list(PLUGIN_GROUPS)
    loaded: dict[str, list[str]] = {}
    for g in groups:
        if g in _discovered and group is None:
            loaded[g] = []
            continue
        ep_group = PLUGIN_GROUPS.get(g)
        if ep_group is None:
            raise err(ErrorCode.PARAM_INVALID, f"未知插件组 {g!r}", hint=f"可用组：{list(PLUGIN_GROUPS)}")
        names: list[str] = []
        for ep in entry_points(group=ep_group):
            try:
                ep.load()
                names.append(ep.name)
            except Exception as exc:  # noqa: BLE001 —— 单个插件坏了不拖垮框架
                names.append(f"{ep.name} (加载失败: {type(exc).__name__})")
        _discovered.add(g)
        loaded[g] = names
    return loaded


def _load_entry_object(value: str) -> Any:
    """加载 ``模块`` 或 ``模块:属性`` 形式的 entry-point 值."""
    if ":" in value:
        module_name, attr = value.split(":", 1)
        module = importlib.import_module(module_name)
        return getattr(module, attr)
    return importlib.import_module(value)


def require_discovered(group: str) -> None:
    """确保某组插件已扫描（供按名查找失败后的兜底调用）。"""
    if group not in _discovered:
        discover_plugins(group)
