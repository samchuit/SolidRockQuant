"""实盘通道测试（假 cfquant 模块，不依赖 QMT 环境）."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import solidrock.config as cfg_mod
from solidrock.agent.errors import ErrorCode, SolidRockError
from solidrock.live import CfquantBroker, diff_positions
from tests.conftest import install_fake_module

XTCONSTANT = SimpleNamespace(STOCK_BUY=23, STOCK_SELL=24, FIX_PRICE=11, LATEST_PRICE=5)

# 测试用守卫配置：放行（网关行为由 TestOrderGuard 专门覆盖）
_PERMISSIVE_GUARD = {
    "live_max_order_notional": 0.0,
    "live_symbol_whitelist": None,
    "live_enforce_trading_hours": False,
    "live_duplicate_window_seconds": 0,
}


class FakeXtAsset:
    total_asset = 500_000.0
    cash = 100_000.0


class FakeXtPosition:
    stock_code = "000001.SZ"
    volume = 1000
    can_use_volume = 800
    avg_price = 10.0
    market_value = 11_500.0


class FakeXtOrder:
    order_id = "OID-1"
    stock_code = "000001.SZ"
    order_volume = 100


class FakeXtTrader:
    def __init__(self, session_id="", account=None, **kwargs):
        self.started = False
        self.account = account

    def start(self) -> None:
        self.started = True

    def query_stock_asset(self, account):
        return FakeXtAsset()

    def query_stock_positions(self, account):
        return [FakeXtPosition()]

    def query_stock_orders(self, account, cancelable_only=False):
        return [FakeXtOrder()]

    def query_stock_trades(self, account):
        return []

    def order_stock(self, account, stock_code, order_type, order_volume, price_type, price, **kw):
        if order_volume % 100 != 0 and order_type == XTCONSTANT.STOCK_BUY:
            return -1
        return "OID-9"

    def cancel_order_stock(self, account, order_id):
        return 0


@pytest.fixture
def cf_env(tmp_path):
    install_fake_module(
        "cfquant",
        xtconstant=XTCONSTANT,
    )
    # cfquant.xttrader / cfquant.xttype 子模块
    install_fake_module(
        "cfquant.xttrader",
        XtQuantTrader=FakeXtTrader,
    )
    install_fake_module(
        "cfquant.xttype",
        StockAccount=lambda account_id, account_type="STOCK", **kw: SimpleNamespace(
            account_id=account_id,
            account_type=account_type,
        ),
    )
    # 环境变量注入账号
    import solidrock.data.sources.registry  # noqa: F401 保持依赖图

    settings = cfg_mod.get_settings()
    original = dict(settings.model_dump())
    settings.live_account_id = "TEST123"
    settings.live_account_type = "STOCK"
    settings.live_read_only = False
    for key, value in _PERMISSIVE_GUARD.items():
        setattr(settings, key, value)
    yield
    for key, value in original.items():
        if hasattr(settings, key):
            setattr(settings, key, value)
    sys.modules.pop("cfquant", None)
    sys.modules.pop("cfquant.xttrader", None)
    sys.modules.pop("cfquant.xttype", None)


def make_broker(tmp_path, *, read_only: bool = False) -> CfquantBroker:
    return CfquantBroker(account_id="TEST123", read_only=read_only, data_dir=tmp_path)


class TestBroker:
    def test_read_only_guard(self, tmp_path: Path, cf_env) -> None:
        broker = make_broker(tmp_path, read_only=True)
        with pytest.raises(SolidRockError) as exc_info:
            broker.submit_order("000001.SZ", "buy", 100)
        assert exc_info.value.code is ErrorCode.LIVE_READ_ONLY
        # 只读模式下查询不受限
        asset = broker.query_asset()
        assert asset["total_asset"] == 500_000.0

    def test_order_type_mapping(self, tmp_path, cf_env) -> None:
        broker = make_broker(tmp_path, read_only=False)
        receipt = broker.submit_order("000001.SZ", "buy", 100)  # 市价
        assert receipt["accepted"] is True
        receipt = broker.submit_order("000001.SZ", "sell", 100, price=11.5)  # 限价
        assert receipt["accepted"] is True

    def test_buy_odd_lot_rejected(self, tmp_path, cf_env) -> None:
        broker = make_broker(tmp_path)
        with pytest.raises(SolidRockError) as exc_info:
            broker.submit_order("000001.SZ", "buy", 150)
        assert exc_info.value.code is ErrorCode.PARAM_INVALID
        # 卖出允许零股（fake trader 对任意买单返回 OID-9，不校验）
        result = broker.submit_order("000001.SZ", "sell", 150)
        assert result["accepted"] is True

    def test_side_validation(self, tmp_path, cf_env) -> None:
        broker = make_broker(tmp_path)
        with pytest.raises(SolidRockError):
            broker.submit_order("000001.SZ", "hold", 100)

    def test_audit_log(self, tmp_path, cf_env) -> None:
        import json

        broker = make_broker(tmp_path)
        broker.submit_order("000001.SZ", "buy", 100)
        audit = (tmp_path / "live" / "audit.jsonl").read_text(encoding="utf-8")
        entries = [json.loads(line) for line in audit.strip().splitlines()]
        assert entries[-1]["action"] == "submit_order"
        assert entries[-1]["account"] == "TEST123"


class TestReconcile:
    def test_diff_actions(self) -> None:
        actions = diff_positions(
            {"000001.SZ": 1000, "600519.SH": 100, "300750.SZ": 0},
            {"000001.SZ": 800, "600519.SH": 100, "300750.SZ": 200},
        )
        by_sym = {a["symbol"]: a for a in actions}
        assert by_sym["000001.SZ"] == {"action": "buy", "symbol": "000001.SZ", "qty": 200}
        assert by_sym["300750.SZ"] == {"action": "sell", "symbol": "300750.SZ", "qty": 200}
        assert "600519.SH" not in by_sym  # 已对平

    def test_diff_buy_rounds_to_lot(self) -> None:
        actions = diff_positions({"000001.SZ": 1150}, {"000001.SZ": 0})
        # 买入按 100 整手取整：1150 → 1100
        assert actions[0]["qty"] == 1100

    def test_markdown_renders(self, tmp_path) -> None:
        from solidrock.live import render_reconcile_markdown

        md = render_reconcile_markdown({"000001.SZ": 800}, {"000001.SZ": 1000}, [])
        assert "实盘对账" in md

    def test_t1_locked_position_not_rebought(self) -> None:
        """总持仓已达标、当日买入不可卖（T+1）时不得建议补仓或卖空."""
        actions = diff_positions(
            {"000001.SZ": 1000},  # 目标
            {"000001.SZ": 1000},  # 总持仓已达标（其中 1000 股为当日买入）
            available={"000001.SZ": 0},
        )
        assert actions == []  # 关键：不能因为"可卖为 0"而误判为缺 1000 股

    def test_sell_limited_by_available(self) -> None:
        """目标减仓但只有部分可卖时，卖单按可用量截断并附说明."""
        actions = diff_positions(
            {"000001.SZ": 0},
            {"000001.SZ": 1000},
            available={"000001.SZ": 300},
        )
        assert actions[0]["action"] == "sell"
        assert actions[0]["qty"] == 300
        assert "T+1" in actions[0]["note"]

    def test_hold_when_nothing_sellable(self) -> None:
        actions = diff_positions({"000001.SZ": 0}, {"000001.SZ": 500}, available={"000001.SZ": 0})
        assert actions[0]["action"] == "hold"
        assert actions[0]["qty"] == 0

    def test_untracked_not_liquidated_by_default(self) -> None:
        """实盘持有但目标未包含的标的默认不清仓，只在 untracked 中提示."""
        from solidrock.live import untracked_positions

        target = {"000001.SZ": 100}
        actual = {"000001.SZ": 100, "600519.SH": 500}
        assert diff_positions(target, actual) == []
        assert untracked_positions(target, actual) == [{"symbol": "600519.SH", "qty": 500}]
        # 显式开启后才给出清仓建议
        forced = diff_positions(target, actual, liquidate_untracked=True)
        assert {a["symbol"]: a["qty"] for a in forced} == {"600519.SH": 500}


class TestOrderGuard:
    """下单守卫：白名单 / 金额上限 / 交易时段 / 幂等（每项都能独立拦截）."""

    @staticmethod
    def _guard(tmp_path, clock, **overrides):
        from solidrock.config import Settings
        from solidrock.live import LiveOrderGuard

        settings = Settings(live_account_id="TEST123", **overrides)
        return LiveOrderGuard(settings=settings, data_dir=tmp_path, clock=clock)

    @staticmethod
    def _at(text: str):
        return lambda: pd.Timestamp(text)

    def test_whitelist_blocks_other_symbol(self, tmp_path) -> None:
        guard = self._guard(
            tmp_path,
            self._at("2026-09-11 10:00"),
            live_symbol_whitelist="510300.SH",
            live_max_order_notional=0.0,
        )
        with pytest.raises(SolidRockError) as exc:
            guard.check("000001.SZ", "buy", 100, price=10.0)
        assert exc.value.code is ErrorCode.LIVE_SYMBOL_NOT_ALLOWED
        guard.check("510300.SH", "buy", 100, price=10.0)  # 白名单内放行

    def test_notional_cap(self, tmp_path) -> None:
        guard = self._guard(tmp_path, self._at("2026-09-11 10:00"), live_max_order_notional=10_000.0)
        with pytest.raises(SolidRockError) as exc:
            guard.check("000001.SZ", "buy", 2000, price=10.0)  # 20,000 > 10,000
        assert exc.value.code is ErrorCode.LIVE_ORDER_TOO_LARGE
        guard.check("000001.SZ", "buy", 1000, price=10.0)  # 10,000 == 上限，放行

    def test_notional_requires_price_reference(self, tmp_path) -> None:
        """市价单且本地无数据 → 无法核验金额，拒绝而非放行（默认配置）."""
        guard = self._guard(tmp_path, self._at("2026-09-11 10:00"))
        with pytest.raises(SolidRockError) as exc:
            guard.check("999999.SZ", "buy", 100, price=None)
        assert exc.value.code is ErrorCode.LIVE_ORDER_TOO_LARGE

    def test_trading_hours_blocked_off_session(self, tmp_path) -> None:
        guard = self._guard(tmp_path, self._at("2026-09-11 21:30"), live_max_order_notional=0.0)
        with pytest.raises(SolidRockError) as exc:
            guard.check("000001.SZ", "buy", 100, price=10.0)
        assert exc.value.code is ErrorCode.LIVE_NOT_TRADING_HOURS

    def test_trading_hours_blocked_weekend(self, tmp_path) -> None:
        guard = self._guard(tmp_path, self._at("2026-09-12 10:00"), live_max_order_notional=0.0)  # 周六
        with pytest.raises(SolidRockError) as exc:
            guard.check("000001.SZ", "buy", 100, price=10.0)
        assert exc.value.code is ErrorCode.LIVE_NOT_TRADING_HOURS

    def test_duplicate_order_deduped(self, tmp_path) -> None:
        guard = self._guard(
            tmp_path,
            self._at("2026-09-11 10:00"),
            live_max_order_notional=0.0,
            live_duplicate_window_seconds=60,
        )
        guard.check("000001.SZ", "buy", 100, price=10.0)
        guard.record("000001.SZ", "buy", 100, price=10.0)
        # 相同指纹 → 命中幂等
        with pytest.raises(SolidRockError) as exc:
            guard.check("000001.SZ", "buy", 100, price=10.0)
        assert exc.value.code is ErrorCode.LIVE_DUPLICATE_ORDER
        # 不同指纹放行
        guard.check("000001.SZ", "buy", 200, price=10.0)

    def test_guard_cannot_be_bypassed_via_broker(self, tmp_path, cf_env) -> None:
        """守卫在 broker 内无条件生效：即使显式 read_only=False 也拦得住."""
        from solidrock.config import Settings
        from solidrock.live import CfquantBroker, LiveOrderGuard

        settings = Settings(
            live_account_id="TEST123",
            live_read_only=False,
            live_max_order_notional=1000.0,
            live_enforce_trading_hours=False,
        )
        guard = LiveOrderGuard(settings=settings, data_dir=tmp_path)
        broker = CfquantBroker(account_id="TEST123", read_only=False, data_dir=tmp_path, guard=guard)
        with pytest.raises(SolidRockError) as exc:
            broker.submit_order("000001.SZ", "buy", 100, price=100.0)  # 10,000 > 1,000
        assert exc.value.code is ErrorCode.LIVE_ORDER_TOO_LARGE

    def test_read_only_inherits_settings_by_default(self, tmp_path, cf_env) -> None:
        """不传 read_only 时继承全局配置（P0：这正是 MCP 工具路径的用法）."""
        from solidrock.live import CfquantBroker

        cfg_mod.get_settings().live_read_only = True
        broker = CfquantBroker(account_id="TEST123", data_dir=tmp_path)
        assert broker.read_only is True
        with pytest.raises(SolidRockError) as exc:
            broker.submit_order("000001.SZ", "buy", 100)
        assert exc.value.code is ErrorCode.LIVE_READ_ONLY
