"""P2.5 测试：策略驱动实盘会话（LiveSession，FakeBroker 替身）."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from solidrock.live.session import LiveSession, SessionConfig
from solidrock.strategy.base import Strategy
from tests.conftest import make_market_store

SYM = "510300.SH"


class FakeBroker:
    """cfquant broker 替身：记录委托、返回固定资金/持仓。"""

    def __init__(self, cash: float = 1_000_000.0, positions: list[dict[str, Any]] | None = None) -> None:
        self.cash = cash
        self.positions = positions or []
        self.submitted: list[dict[str, Any]] = []

    def query_asset(self) -> dict[str, Any]:
        return {"m_dCash": self.cash, "total_value": self.cash}

    def query_positions(self) -> list[dict[str, Any]]:
        return self.positions

    def submit_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        *,
        price: float | None = None,
        strategy_name: str = "solidrock",
        order_remark: str = "",
    ) -> dict[str, Any]:
        self.submitted.append({"symbol": symbol, "side": side, "qty": qty, "price": price})
        return {"order_id": len(self.submitted), "accepted": True}


class LiveMA(Strategy):
    """动量测试策略：最新收盘 > n 日均线 → 持有 50%，否则清仓。"""

    params = {"n": 5, "symbol": SYM}

    def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
        ctx.universe = [str(self.params["symbol"])]

    def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
        symbol = str(self.params["symbol"])
        close = ctx.history(symbol, int(self.params["n"]), fields="close")[symbol]
        if close.iloc[-1] > close.mean():
            ctx.order_target_percent(symbol, 0.5)
        else:
            ctx.order_target_percent(symbol, 0.0)


@pytest.fixture
def session_store(tmp_path: Path, clean_settings, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SOLIDROCK_DATA_DIR", str(tmp_path / "data"))
    return make_market_store(tmp_path / "mkt", symbols=(SYM,), days=30, base=10.0, drift=0.1, benchmark=None)


class TestLiveSession:
    def test_signal_phase_dry_run(self, session_store) -> None:
        broker = FakeBroker()
        session = LiveSession(LiveMA(), session_store, broker)
        summary = session.run_phase("signal", pd.Timestamp("2026-09-11 14:50"), execute=False)
        assert summary["universe"] == [SYM]
        assert len(summary["orders"]) == 1
        order = summary["orders"][0]
        assert order["side"] == "buy"
        assert order["qty"] > 0 and order["qty"] % 100 == 0  # 买入整手
        assert order["submitted"] is False
        assert broker.submitted == []  # dry-run 未触达 broker

    def test_signal_phase_execute(self, session_store, tmp_path: Path) -> None:
        broker = FakeBroker()
        session = LiveSession(LiveMA(), session_store, broker)
        summary = session.run_phase("signal", pd.Timestamp("2026-09-11 14:50"), execute=True)
        assert len(broker.submitted) == 1
        sent = broker.submitted[0]
        assert sent["side"] == "buy" and sent["qty"] % 100 == 0
        assert summary["orders"][0]["submitted"] is True
        # 产物落盘（SOLIDROCK_DATA_DIR 已隔离到 tmp）
        out = tmp_path / "data" / "live" / "sessions"
        files = list(out.glob("*_signal.json"))
        assert len(files) == 1
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        assert payload["phase"] == "signal"

    def test_portfolio_snapshot_drives_sell(self, session_store) -> None:
        """已有持仓 + 动量转负 → 产出卖出意图（卖出不要求整手）。"""
        broker = FakeBroker(positions=[{"stock_code": SYM, "volume": 1234, "can_use_volume": 1234}])

        class ExitAll(Strategy):
            params = {"symbol": SYM}

            def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
                ctx.universe = [str(self.params["symbol"])]

            def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
                ctx.order_target_percent(str(self.params["symbol"]), 0.0)

        session = LiveSession(ExitAll(), session_store, broker)
        summary = session.run_phase("signal", pd.Timestamp("2026-09-11 14:50"), execute=False)
        order = summary["orders"][0]
        assert order["side"] == "sell"
        assert order["qty"] == 1234  # 卖出允许零股清仓

    def test_hook_phases(self, session_store) -> None:
        calls: list[str] = []

        class HookProbe(Strategy):
            params: dict = {}

            def setup(self, ctx) -> None:  # type: ignore[no-untyped-def]
                ctx.universe = [SYM]

            def on_market_open(self, ctx) -> None:  # type: ignore[no-untyped-def]
                calls.append("open")

            def on_signal(self, ctx) -> None:  # type: ignore[no-untyped-def]
                calls.append("signal")

            def on_market_close(self, ctx) -> None:  # type: ignore[no-untyped-def]
                calls.append("close")

        session = LiveSession(HookProbe(), session_store, FakeBroker())
        session.run_phase("open", pd.Timestamp("2026-09-11"), execute=False)
        session.run_phase("signal", pd.Timestamp("2026-09-11 14:50"), execute=False)
        session.run_phase("close", pd.Timestamp("2026-09-11"), execute=False)
        assert calls == ["open", "signal", "close"]

    def test_execute_without_broker(self, session_store) -> None:
        session = LiveSession(LiveMA(), session_store, None)
        summary = session.run_phase("signal", pd.Timestamp("2026-09-11 14:50"), execute=True)
        assert summary["orders"][0]["note"] == "NO_BROKER"

    def test_limit_price_option(self, session_store) -> None:
        broker = FakeBroker()
        session = LiveSession(LiveMA(), session_store, broker, config=SessionConfig(limit_pct=0.001))
        summary = session.run_phase("signal", pd.Timestamp("2026-09-11 14:50"), execute=True)
        sent = broker.submitted[0]
        assert sent["price"] is not None  # 限价 = 最近收盘 × (1 + 0.001)
        assert sent["price"] == pytest.approx(summary["orders"][0]["price"])

    def test_invalid_phase(self, session_store) -> None:
        from solidrock.agent.errors import SolidRockError

        session = LiveSession(LiveMA(), session_store, None)
        with pytest.raises(SolidRockError):
            session.run_phase("noon", pd.Timestamp("2026-09-11"), execute=False)
