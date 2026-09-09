"""插件机制测试（假 entry-points，不依赖真实第三方包）."""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from solidrock.agent.errors import ErrorCode, SolidRockError
from solidrock.plugins import PLUGIN_GROUPS, discover_plugins
from tests.conftest import install_fake_module


class _FakeSelection:
    """模拟 entry_points(group=...) 的返回（按组过滤、可迭代）。"""

    def __init__(self, entries: list, group: str) -> None:
        self._entries = [e for e in entries if e.group == group]

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._entries)


@pytest.fixture
def plugin_env(monkeypatch: pytest.MonkeyPatch):
    """安装假插件模块 + 假 entry-points，测试后清理注册表。"""
    from solidrock.data.schema import DAILY_BAR_COLUMN_NAMES
    from solidrock.data.sources import Capability, DataSource, register_source
    from solidrock.factors import Factor, register_factor

    @register_source
    class MySource(DataSource):
        name = "my-plug-source"
        capabilities = frozenset({Capability.BARS_DAILY_STOCK})

        @classmethod
        def is_available(cls) -> bool:
            return True

        def _fetch_bars_one(self, symbol, start, end, *, with_adj_factor):  # type: ignore[no-untyped-def]
            return pd.DataFrame(columns=list(DAILY_BAR_COLUMN_NAMES))

    @register_factor
    class MyMomentum(Factor):
        lookback = 5

        def compute(self, data):  # type: ignore[no-untyped-def]
            return data.hfq_close().pct_change(5)

    install_fake_module("myplug.sources", MySource=MySource)
    install_fake_module("myplug.factors", MyMomentum=MyMomentum)

    eps = [
        _FakeEP("my-plug-source", "solidrock.sources", "myplug.sources"),
        _FakeEP("MyMomentum", "solidrock.factors", "myplug.factors:MyMomentum"),
    ]
    monkeypatch.setattr(
        "solidrock.plugins.entry_points",
        lambda **kw: _FakeSelection(eps, kw.get("group", "")),  # type: ignore[arg-type]
    )
    from solidrock import plugins

    plugins._discovered.clear()
    yield eps
    plugins._discovered.clear()
    sys.modules.pop("myplug.sources", None)
    sys.modules.pop("myplug.factors", None)
    from solidrock.data.sources.registry import _REGISTRY
    from solidrock.factors.base import _FACTOR_REGISTRY

    _REGISTRY.pop("my-plug-source", None)
    _FACTOR_REGISTRY.pop("MyMomentum", None)


class _FakeEP:
    def __init__(self, name: str, group: str, value: str) -> None:
        self.name = name
        self.group = group
        self.value = value

    def load(self):  # type: ignore[no-untyped-def]
        if ":" in self.value:
            module_name, attr = self.value.split(":", 1)
            return getattr(importlib.import_module(module_name), attr)
        return importlib.import_module(self.value)


class TestPluginDiscovery:
    def test_discover_returns_names(self, plugin_env) -> None:
        result = discover_plugins()
        assert result["sources"] == ["my-plug-source"]
        assert result["factors"] == ["MyMomentum"]

    def test_plugin_source_usable(self, plugin_env) -> None:
        from solidrock.data.sources import create_source

        src = create_source("my-plug-source")
        assert src.name == "my-plug-source"

    def test_plugin_factor_usable(self, plugin_env) -> None:
        from solidrock.factors import create_factor

        factor = create_factor("MyMomentum")
        assert factor.name == "MyMomentum"

    def test_unknown_group_rejected(self, plugin_env) -> None:
        with pytest.raises(SolidRockError) as exc_info:
            discover_plugins("bogus")
        assert exc_info.value.code is ErrorCode.PARAM_INVALID

    def test_broken_plugin_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from solidrock import plugins

        def boom():  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

        eps = [SimpleNamespace(name="broken", group="solidrock.sources", value="x", load=boom)]
        monkeypatch.setattr(
            "solidrock.plugins.entry_points",
            lambda **kw: _FakeSelection(eps, kw.get("group", "")),  # type: ignore[arg-type]
        )
        plugins._discovered.clear()
        try:
            result = discover_plugins("sources")
            assert "加载失败" in result["sources"][0]
        finally:
            plugins._discovered.clear()

    def test_groups_declared(self) -> None:
        assert set(PLUGIN_GROUPS) == {"sources", "factors"}
