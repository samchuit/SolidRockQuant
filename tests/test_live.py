"""实盘通道测试（假 cfquant 模块，不依赖 QMT 环境）."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from solidrock.agent.errors import ErrorCode, SolidRockError
from solidrock.live import CfquantBroker, diff_positions
from tests.conftest import install_fake_module

XTCONSTANT = SimpleNamespace(STOCK_BUY=23, STOCK_SELL=24, FIX_PRICE=11, LATEST_PRICE=5)


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
    import solidrock.config as cfg_mod
    import solidrock.data.sources.registry  # noqa: F401 保持依赖图

    original = dict(cfg_mod.get_settings().model_dump())
    cfg_mod.get_settings().live_account_id = "TEST123"
    cfg_mod.get_settings().live_account_type = "STOCK"
    cfg_mod.get_settings().live_read_only = False
    yield
    cfg_mod.get_settings().live_read_only = original.get("live_read_only", True)
    cfg_mod.get_settings().live_account_id = original.get("live_account_id")
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
