"""MCP 工具层测试：信封结构、JSON 兼容、端到端工具调用."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from solidrock.agent.tools import ALL_TOOLS, json_safe, tool_get_data_overview, tool_validate_strategy
from solidrock.backtest.costs import AShareCostModel
from solidrock.data.sources.base import Capability, DataSource
from solidrock.data.sources.registry import register_source
from solidrock.experiments.tracker import ExperimentTracker
from tests.conftest import make_market_store


class StubSource(DataSource):
    name = "stub-mcp"
    capabilities = frozenset({Capability.BARS_DAILY_STOCK, Capability.INSTRUMENTS_STOCK})

    def _fetch_bars_one(self, symbol, start, end, *, with_adj_factor):  # type: ignore[no-untyped-def]
        dates = pd.bdate_range("2024-01-02", periods=8)
        closes = 10.0 + 0.1 * np.arange(8)
        from tests.conftest import bars_frame

        df = bars_frame(symbol.value, dates, closes)
        if start is not None:
            df = df[df["date"] >= start]
        return df

    def _fetch_instruments(self, asset_type):  # type: ignore[no-untyped-def]
        return pd.DataFrame(
            {"symbol": ["000001.SZ", "600519.SH", "510300.SH"], "name": ["平安银行", "贵州茅台", "沪深300ETF"]}
        )


@pytest.fixture
def mcp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    register_source(StubSource)
    make_market_store(tmp_path, symbols=("510300.SH",), days=8, benchmark="000300.SH")
    monkeypatch.setenv("SOLIDROCK_DATA_DIR", str(tmp_path / "mkt"))
    from solidrock.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


STRATEGY_FILE = """
from solidrock import Strategy

class DemoMA(Strategy):
    params = {"symbol": "510300.SH"}

    def setup(self, ctx) -> None:
        ctx.universe = [str(self.params["symbol"])]

    def on_signal(self, ctx) -> None:
        symbol = str(self.params["symbol"])
        if ctx.position(symbol) == 0:
            close = ctx.history(symbol, 5, fields="close")[symbol]
            if close.isna().any():
                return
            ctx.order_target_percent(symbol, 1.0)
"""


def _parse(result: str) -> dict:
    envelope = json.loads(result)
    assert set(envelope) == {"status", "data", "artifacts", "next_suggested_tools", "error"}
    return envelope


class TestEnvelopes:
    def test_data_overview(self, mcp_env: Path) -> None:
        env = _parse(tool_get_data_overview())
        assert env["status"] == "ok"
        assert env["data"]["bars"]["1d"]["n_symbols"] == 2
        assert env["data"]["calendar"]["n_days"] == 8

    def test_fetch_bars_updates_store(self, mcp_env: Path) -> None:
        result = ALL_TOOLS["fetch_bars"](symbols=["000001.SZ"], start="2024-01-02", source="stub-mcp")
        env = _parse(result)
        assert env["status"] == "ok"
        assert env["data"]["symbols"]["000001.SZ"]["rows_in_store"] == 8

    def test_search_instruments_auto_fetch(self, mcp_env: Path) -> None:
        env = _parse(ALL_TOOLS["search_instruments"](query="茅台", source="stub-mcp"))
        assert env["status"] == "ok"
        assert env["data"]["matches"][0]["symbol"] == "600519.SH"

    def test_validate_strategy_envelope(self, mcp_env: Path, tmp_path: Path) -> None:
        good = tmp_path / "good.py"
        good.write_text(STRATEGY_FILE, encoding="utf-8")
        env = _parse(tool_validate_strategy(strategy_file=str(good)))
        assert env["status"] == "ok"
        assert env["data"]["strategy_classes"] == ["DemoMA"]

        bad = tmp_path / "bad.py"
        bad.write_text("close.shift(-1)\n", encoding="utf-8")
        env = _parse(tool_validate_strategy(strategy_file=str(bad)))
        assert env["status"] == "error"
        assert env["error"]["code"] == "STRATEGY_INVALID"
        assert env["error"]["details"]["issues"]

    def test_run_backtest_e2e(self, mcp_env: Path, tmp_path: Path) -> None:
        strategy = tmp_path / "demo.py"
        strategy.write_text(STRATEGY_FILE, encoding="utf-8")
        env = _parse(
            ALL_TOOLS["run_backtest"](
                strategy_file=str(strategy),
                start="2024-01-02",
                end="2024-01-19",
                params={"symbol": "510300.SH"},
                benchmark=None,
                name="mcp-e2e",
            )
        )
        assert env["status"] == "ok", env
        data = env["data"]
        assert data["strategy"] == "DemoMA"
        assert "sharpe" in data["metrics"]
        assert len(data["artifacts"]) == 5  # html/md/json/trades/nav（plotly 可用时含 report.html）
        assert all(Path(p).exists() for p in data["artifacts"])
        assert any(p.endswith("report.html") for p in data["artifacts"])
        # 自动留痕
        tracker = ExperimentTracker(mcp_env / "mkt" / "experiments.db")
        assert tracker.get_run(data["run_id"])["name"] == "mcp-e2e"

    def test_experiment_tools(self, mcp_env: Path) -> None:
        tracker = ExperimentTracker(mcp_env / "mkt" / "experiments.db")
        id1 = tracker.log_run(kind="backtest", name="A", config={}, metrics={"sharpe": 1.0})
        id2 = tracker.log_run(kind="backtest", name="B", config={}, metrics={"sharpe": 0.5})

        env = _parse(ALL_TOOLS["list_experiments"](kind="backtest"))
        assert env["data"]["n_runs"] == 2

        env = _parse(ALL_TOOLS["get_experiment"](run_id=id1))
        assert env["data"]["metrics"]["sharpe"] == 1.0

        env = _parse(ALL_TOOLS["compare_experiments"](run_ids=[id1, id2]))
        assert env["data"]["n_runs"] == 2

        env = _parse(ALL_TOOLS["get_experiment"](run_id="missing"))
        assert env["status"] == "error"
        assert env["error"]["code"] == "NO_DATA"
        assert env["error"]["hint"]

    def test_unexpected_exception_becomes_internal_error(self, mcp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(**kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("surprise")

        monkeypatch.setattr("solidrock.agent.tools._data_overview", boom)
        env = _parse(tool_get_data_overview())
        assert env["status"] == "error"
        assert env["error"]["code"] == "INTERNAL_ERROR"


class TestJsonSafe:
    def test_nan_inf_become_none(self) -> None:
        cleaned = json_safe({"a": float("nan"), "b": float("inf"), "c": 1.5, "d": [float("nan")], "e": "x"})
        assert cleaned == {"a": None, "b": None, "c": 1.5, "d": [None], "e": "x"}

    def test_envelope_always_strict_json(self, mcp_env: Path) -> None:
        envelope = json.loads(tool_get_data_overview())
        # allow_nan=False 的序列化不会抛错
        json.dumps(envelope, allow_nan=False)


class TestLiveToolSafety:
    """MCP 下单路径的安全闸门（在**工具调用层**验证，而非只测 broker 类）.

    历史缺口：测试只覆盖了 ``CfquantBroker(read_only=True)``，而工具层硬编码
    ``read_only=False``，于是"默认只读"名存实亡却测试全绿。以下测试直接打工具层。
    """

    def test_submit_requires_explicit_confirm(self, mcp_env: Path) -> None:
        from solidrock.agent.tools import tool_live_submit_order

        env = _parse(tool_live_submit_order("510300.SH", "buy", 100))
        assert env["status"] == "error"
        assert env["error"]["code"] == "PARAM_INVALID"
        assert "confirm" in env["error"]["message"]

    def test_submit_blocked_by_read_only_config(self, mcp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """只读配置必须真正拦住 MCP 下单（无须 cfquant 在线即应拒绝）."""
        from solidrock.agent.tools import tool_live_submit_order
        from solidrock.config import get_settings

        monkeypatch.setenv("SOLIDROCK_LIVE_READ_ONLY", "true")
        monkeypatch.setenv("SOLIDROCK_LIVE_ACCOUNT_ID", "TEST123")
        get_settings.cache_clear()
        try:
            env = _parse(tool_live_submit_order("510300.SH", "buy", 100, confirm=True))
            assert env["status"] == "error"
            assert env["error"]["code"] == "LIVE_READ_ONLY"
        finally:
            get_settings.cache_clear()

    def test_submit_reaches_guard_when_read_only_off(self, mcp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """关只读后仍被下单守卫拦住（金额上限），证明守卫在工具路径同样生效."""
        from solidrock.agent.tools import tool_live_submit_order
        from solidrock.config import get_settings

        monkeypatch.setenv("SOLIDROCK_LIVE_READ_ONLY", "false")
        monkeypatch.setenv("SOLIDROCK_LIVE_ACCOUNT_ID", "TEST123")
        monkeypatch.setenv("SOLIDROCK_LIVE_ENFORCE_TRADING_HOURS", "false")
        monkeypatch.setenv("SOLIDROCK_LIVE_MAX_ORDER_NOTIONAL", "1000")
        get_settings.cache_clear()
        try:
            env = _parse(tool_live_submit_order("510300.SH", "buy", 100, price=100.0, confirm=True))
            assert env["status"] == "error"
            assert env["error"]["code"] == "LIVE_ORDER_TOO_LARGE"
        finally:
            get_settings.cache_clear()


def _cost_model_placeholder() -> None:
    AShareCostModel(commission_rate=0)


class TestServerBuilds:
    def test_build_server_registers_all_tools(self) -> None:
        from solidrock.agent.mcp_server import build_server

        server = build_server()
        # FastMCP 实例且注册了全部工具
        registered = set(server._tool_manager._tools.keys())
        assert set(ALL_TOOLS) <= registered
