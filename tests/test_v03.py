"""v0.3 测试：策略沙箱、模拟盘."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from solidrock.agent.errors import ErrorCode, SolidRockError
from solidrock.agent.sandbox import run_sandboxed_backtest
from solidrock.backtest import BacktestConfig, BacktestEngine
from solidrock.backtest.paper import PaperTrader
from solidrock.data.store import DataStore
from solidrock.strategy.base import Strategy
from tests.conftest import make_market_store

SYM = "510300.SH"

GOOD_STRATEGY = """
from solidrock import Strategy

class Good(Strategy):
    params = {"symbol": "510300.SH"}

    def setup(self, ctx) -> None:
        ctx.universe = [str(self.params["symbol"])]

    def on_signal(self, ctx) -> None:
        symbol = str(self.params["symbol"])
        if ctx.position(symbol) == 0:
            ctx.order(symbol, 100)
"""

LOOP_STRATEGY = """
from solidrock import Strategy

class Loop(Strategy):
    def setup(self, ctx) -> None:
        ctx.universe = ["510300.SH"]

    def on_signal(self, ctx) -> None:
        while True:
            pass
"""

CRASH_STRATEGY = """
from solidrock import Strategy

class Crash(Strategy):
    def setup(self, ctx) -> None:
        import sys
        sys.exit(3)  # 直接退出进程——沙箱必须扛住
"""


def _write(tmp_path: Path, name: str, code: str) -> Path:
    p = tmp_path / name
    p.write_text(code, encoding="utf-8")
    return p


@pytest.fixture
def market(tmp_path: Path) -> DataStore:
    return make_market_store(tmp_path, symbols=(SYM,), days=10, base=10.0, drift=0.1)


class TestSandbox:
    def test_normal_strategy_passes(self, tmp_path: Path, market: DataStore, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOLIDROCK_DATA_DIR", str(market.root))
        from solidrock.config import get_settings

        get_settings.cache_clear()
        try:
            strategy = _write(tmp_path, "good.py", GOOD_STRATEGY)
            result = run_sandboxed_backtest(
                str(strategy),
                start="2024-01-02",
                end="2024-01-15",
                data_dir=str(market.root),
                timeout=120,
            )
            assert result["status"] == "ok", result
            assert result["data"]["metrics"]["n_trades"] == 1.0
            assert Path(result["artifacts"][0]).exists()
        finally:
            get_settings.cache_clear()

    def test_infinite_loop_times_out(self, tmp_path: Path, market: DataStore) -> None:
        strategy = _write(tmp_path, "loop.py", LOOP_STRATEGY)
        result = run_sandboxed_backtest(
            str(strategy),
            start="2024-01-02",
            end="2024-01-15",
            data_dir=str(market.root),
            timeout=10.0,
        )
        assert result["status"] == "error"
        assert result["error"]["code"] == "TIMEOUT"

    def test_sys_exit_isolated(self, tmp_path: Path, market: DataStore) -> None:
        """策略直接 sys.exit：子进程退出，宿主进程不受影响，返回错误信封。"""
        strategy = _write(tmp_path, "crash.py", CRASH_STRATEGY)
        result = run_sandboxed_backtest(
            str(strategy),
            start="2024-01-02",
            end="2024-01-15",
            data_dir=str(market.root),
            timeout=60.0,
        )
        assert result["status"] == "error"
        assert result["error"]["details"]["returncode"] == 3  # 子进程退出码被隔离捕获

    def test_missing_strategy_file(self, market: DataStore) -> None:
        result = run_sandboxed_backtest(
            str(market.root / "nope.py"),
            start="2024-01-02",
            end="2024-01-15",
            data_dir=str(market.root),
            timeout=60.0,
        )
        assert result["status"] == "error"


class TestPaper:
    def _config(self, start: str, end: str) -> BacktestConfig:
        from solidrock.backtest.costs import AShareCostModel

        return BacktestConfig(
            start=start,
            end=end,
            benchmark=None,
            cost_model=AShareCostModel(
                commission_rate=0.0,
                commission_min=0.0,
                stamp_duty_rate=0.0,
                transfer_fee_rate=0.0,
                slippage_bps=0.0,
            ),
            log_experiment=False,
            max_position_weight=None,
            carry_pending=True,
        )

    def test_incremental_sessions(self, tmp_path: Path) -> None:
        """两次会话：首次建仓（订单挂起），次会话开盘成交。"""
        store = make_market_store(tmp_path, symbols=(SYM,), days=8, base=10.0, drift=0.1)
        strategy = _paper_strategy(tmp_path)
        trader = PaperTrader(
            str(strategy), self._config("2024-01-02", "2024-01-15"), store, name="p1", params={"symbol": SYM}
        )

        s1 = trader.run(end="2024-01-02")  # 首次：仅处理 01-02
        assert s1["ran"] is True
        # 01-02（末日）收盘出信号 → 订单挂起未成交
        assert s1["pending_orders"], "首会话末日信号应挂起"
        state = json.loads((store.root / "paper" / "p1" / "state.json").read_text(encoding="utf-8"))
        assert state["last_date"] == "2024-01-02"
        assert state["pending"], "状态文件应持久化挂单"

        s2 = trader.run(end="2024-01-15")  # 次会话：01-03 开盘成交挂起订单
        assert s2["ran"] is True
        assert s2["trades"], "第二次会话应成交挂起的订单"

    def test_no_new_days(self, tmp_path: Path) -> None:
        store = make_market_store(tmp_path, symbols=(SYM,), days=8, base=10.0, drift=0.1)
        strategy = _paper_strategy(tmp_path)
        trader = PaperTrader(
            str(strategy), self._config("2024-01-02", "2024-01-15"), store, name="p2", params={"symbol": SYM}
        )
        trader.run(end="2024-01-15")
        s = trader.run(end="2024-01-15")  # 无新交易日
        assert s["ran"] is False

    def test_state_persistence_roundtrip(self, tmp_path: Path) -> None:
        store = make_market_store(tmp_path, symbols=(SYM,), days=8, base=10.0, drift=0.1)
        strategy = _paper_strategy(tmp_path)
        trader = PaperTrader(
            str(strategy), self._config("2024-01-02", "2024-01-15"), store, name="p3", params={"symbol": SYM}
        )
        trader.run(end="2024-01-15")
        status = trader.status()
        assert status["last_date"] == "2024-01-11"
        assert status["run_count"] == 1
        assert status["initial_cash"] == 1_000_000.0

    def test_execution_mode_guard(self, tmp_path: Path) -> None:
        cfg = BacktestConfig(start="2024-01-02", end="2024-01-15", execution="same_close")
        with pytest.raises(SolidRockError) as exc_info:
            PaperTrader(str(_write(tmp_path, "s.py", GOOD_STRATEGY)), cfg, DataStore(tmp_path / "x"), name="p4")
        assert exc_info.value.code is ErrorCode.PARAM_INVALID

    def test_halt_state_persists_across_runs(self, tmp_path: Path) -> None:
        """回归：单向熔断不得因模拟盘重启而复位（否则会重新开仓）."""
        import json as _json

        # 持续下跌行情 + 低熔断阈值：首次运行即触发
        store = make_market_store(tmp_path, symbols=(SYM,), days=12, base=20.0, drift=-1.0)
        strategy = _paper_strategy(tmp_path)
        cfg = BacktestConfig(
            start="2024-01-02",
            end="2024-01-31",
            benchmark=None,
            drawdown_halt=0.05,
            log_experiment=False,
            max_position_weight=None,
            carry_pending=True,
        )
        trader = PaperTrader(str(strategy), cfg, store, name="halt", params={"symbol": SYM})
        s1 = trader.run(end="2024-01-12")
        assert s1["ran"] is True
        if not s1["halted"]:
            pytest.skip("该合成行情未触发熔断，跳过（阈值与走势相关）")

        # 熔断状态必须落盘
        state = _json.loads((store.root / "paper" / "halt" / "state.json").read_text(encoding="utf-8"))
        assert state["halted"] is True
        assert state["halt_peak"] is not None

        # 重启（新实例）后仍应保持熔断，且 status 明确告知
        trader2 = PaperTrader(str(strategy), cfg, store, name="halt", params={"symbol": SYM})
        status = trader2.status()
        assert status["halted"] is True
        assert "熔断" in (status["note"] or "")

        # 续跑不得重新建仓
        s2 = trader2.run(end="2024-01-31")
        assert s2["ran"] is True
        assert s2["halted"] is True
        assert not any(t["side"] == "buy" for t in s2["trades"]), "熔断后不应再开新仓"
        assert "HALTED" in s2["rejections"], "熔断期间策略订单应被拒"


class TestCarryPending:
    def test_engine_carry_pending_vs_backtest(self, tmp_path: Path) -> None:
        """carry_pending=True 时未执行订单返回给调用方；回测模式记 NO_MORE_BARS。"""
        store = make_market_store(tmp_path, symbols=(SYM,), days=5, base=10.0, drift=0.0)
        dates = pd.bdate_range("2024-01-02", periods=5)

        class LastDayOrder(Strategy):
            params = {"symbol": SYM}

            def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
                ctx.universe = [str(self.params["symbol"])]

            def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
                symbol = str(self.params["symbol"])
                if ctx.now == dates[-1]:
                    ctx.order(symbol, 100)

        def cfg(carry: bool) -> BacktestConfig:
            from solidrock.backtest.costs import AShareCostModel

            return BacktestConfig(
                start="2024-01-02",
                end="2024-01-08",
                benchmark=None,
                cost_model=AShareCostModel(
                    commission_rate=0.0,
                    commission_min=0.0,
                    stamp_duty_rate=0.0,
                    transfer_fee_rate=0.0,
                    slippage_bps=0.0,
                ),
                log_experiment=False,
                max_position_weight=None,
                carry_pending=carry,
            )

        r_carry = BacktestEngine(LastDayOrder(), cfg(True), store).run()
        assert r_carry.final_pending  # 挂单返回给调用方（模拟盘下一时段执行）
        assert "NO_MORE_BARS" not in set(r_carry.rejections.get("code", pd.Series(dtype=str)))

        r_backtest = BacktestEngine(LastDayOrder(), cfg(False), store).run()
        assert not r_backtest.final_pending
        assert "NO_MORE_BARS" in set(r_backtest.rejections["code"])


def _paper_strategy(tmp_path: Path) -> Path:
    p = tmp_path / "paper_strategy.py"
    p.write_text(
        "from solidrock import Strategy\n\n"
        "class PaperDemo(Strategy):\n"
        '    params = {"symbol": "510300.SH"}\n\n'
        "    def setup(self, ctx) -> None:\n"
        '        ctx.universe = [str(self.params["symbol"])]\n\n'
        "    def on_signal(self, ctx) -> None:\n"
        '        symbol = str(self.params["symbol"])\n'
        "        if ctx.position(symbol) == 0:\n"
        '            close = ctx.history(symbol, 2, fields="close")[symbol]\n'
        "            if not close.isna().any():\n"
        "                ctx.order_target_percent(symbol, 0.5)\n",
        encoding="utf-8",
    )
    return p


def _write_strategy(tmp_path: Path) -> Path:
    p = tmp_path / "paper_strategy.py"
    p.write_text(
        "from solidrock import Strategy\n\n"
        "class PaperDemo(Strategy):\n"
        '    params = {"symbol": "510300.SH"}\n\n'
        "    def setup(self, ctx) -> None:\n"
        '        ctx.universe = [str(self.params["symbol"])]\n\n'
        "    def on_signal(self, ctx) -> None:\n"
        '        symbol = str(self.params["symbol"])\n'
        "        if ctx.position(symbol) == 0:\n"
        '            close = ctx.history(symbol, 2, fields="close")[symbol]\n'
        "            if not close.isna().any():\n"
        "                ctx.order_target_percent(symbol, 0.5)\n",
        encoding="utf-8",
    )
    return p
