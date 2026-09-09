r"""实盘 Broker：cfquant（大 QMT 桥接）封装.

cfquant 提供与 ``xtquant`` 兼容的本地 API（见其 docs/ai-skill），本模块把它
封装为 SolidRockQuant 的实盘通道：

- **符号**：QMT 代码格式（``000001.SZ``）与框架统一符号一致，无需转换；
- **下单**：``submit_order`` 统一 buy/sell 语义，市价（LATEST_PRICE）或限价
  （FIX_PRICE）；数量自动按整手校验（买入 100 股倍数，卖出允许零股清仓）；
- **只读守卫**：默认 ``read_only=True``（配置 ``SOLIDROCK_LIVE_READ_ONLY``），
  下单/撤单直接拒绝；实盘下单需要显式关闭只读并二次确认；
- **审计**：每笔下单/撤单追加写入 ``{data_dir}/live/audit.jsonl``。

依赖：``cfquant``（D:\cfquant 源码安装或 pip install cfquant）。未安装时抛
``LIVE_UNAVAILABLE``（带安装 hint）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from solidrock.agent.errors import ErrorCode, err
from solidrock.config import get_settings


def _import_cfquant() -> dict[str, Any]:
    """惰性导入 cfquant；未安装时给安装 hint."""
    try:
        from cfquant import xtconstant
        from cfquant.xttrader import XtQuantTrader
        from cfquant.xttype import StockAccount

        return {"xtconstant": xtconstant, "XtQuantTrader": XtQuantTrader, "StockAccount": StockAccount}
    except ImportError as exc:
        raise err(
            ErrorCode.LIVE_UNAVAILABLE,
            "实盘通道 cfquant 未安装",
            hint="源码部署：pip install -e D:/cfquant；或 pip install cfquant；"
            "并确认 QMT 已登录、cfquant 桥接策略已运行（Web 控制台可查）",
        ) from exc


class CfquantBroker:
    """cfquant（QMT）实盘通道封装."""

    def __init__(
        self,
        *,
        account_id: str | None = None,
        account_type: str | None = None,
        read_only: bool | None = None,
        data_dir: str | Path | None = None,
    ) -> None:
        settings = get_settings()
        self.account_id = account_id or settings.live_account_id
        self.account_type = account_type or settings.live_account_type
        self.read_only = settings.live_read_only if read_only is None else read_only
        if not self.account_id:
            raise err(
                ErrorCode.LIVE_UNAVAILABLE,
                "实盘资金账号未配置",
                hint="设置环境变量 SOLIDROCK_LIVE_ACCOUNT_ID=<资金账号>（写入 .env 即可，不入 git）",
            )
        self.data_dir = Path(data_dir) if data_dir else settings.resolved_data_dir()
        self._cf: Any = None
        self._account: Any = None
        self._trader: Any = None
        self._connected = False

    # ------------------------------------------------------------------ 连接
    def _connect(self) -> None:
        if self._connected:
            return
        self._cf = _import_cfquant()
        self._account = self._cf["StockAccount"](self.account_id, self.account_type)
        self._trader = self._cf["XtQuantTrader"]("", account=self._account)
        self._trader.start()
        self._connected = True

    def _ensure_writable(self) -> None:
        if self.read_only:
            raise err(
                ErrorCode.LIVE_READ_ONLY,
                "实盘通道处于只读模式，禁止下单/撤单",
                hint="确认风险后设置环境变量 SOLIDROCK_LIVE_READ_ONLY=false（或 CLI --no-read-only）",
            )

    # ------------------------------------------------------------------ 查询
    def query_asset(self) -> dict[str, Any]:
        """查询账户资金（原始字段透传，字段以 QMT 返回为准）。"""
        self._connect()
        asset = self._trader.query_stock_asset(self._account)
        return self._wrap(asset, "asset")

    def query_positions(self) -> list[dict[str, Any]]:
        """查询全部持仓。"""
        self._connect()
        positions = self._trader.query_stock_positions(self._account)
        return [self._wrap(p, "position") for p in positions or []]

    def query_orders(self, *, cancelable_only: bool = False) -> list[dict[str, Any]]:
        """查询委托（可只查可撤委托）。"""
        self._connect()
        orders = self._trader.query_stock_orders(self._account, cancelable_only=cancelable_only)
        return [self._wrap(o, "order") for o in orders or []]

    def query_trades(self) -> list[dict[str, Any]]:
        """查询成交。"""
        self._connect()
        trades = self._trader.query_stock_trades(self._account)
        return [self._wrap(t, "trade") for t in trades or []]

    # ------------------------------------------------------------------ 交易
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
        """提交实盘订单.

        - ``side``：``buy`` / ``sell``；
        - ``qty``：股数（买入自动校验 100 股整手；卖出允许零股清仓）；
        - ``price``：None → 对手方最新价（LATEST_PRICE）；给定 → 限价（FIX_PRICE）。

        返回 ``{"order_id": ..., "echo": {...下单回执字段...}}``，并写入审计日志。
        """
        self._connect()
        self._ensure_writable()
        if side not in ("buy", "sell"):
            raise err(ErrorCode.PARAM_INVALID, f"side 应为 buy/sell，收到 {side!r}")
        qty = int(qty)
        if qty <= 0:
            raise err(ErrorCode.PARAM_INVALID, f"数量必须为正，收到 {qty}")
        if side == "buy" and qty % 100 != 0:
            raise err(
                ErrorCode.PARAM_INVALID,
                f"买入数量必须为 100 股整手，收到 {qty}",
                hint="卖出（含清仓零股）不受整手限制",
            )
        xtconstant = self._cf["xtconstant"]
        order_type = xtconstant.STOCK_BUY if side == "buy" else xtconstant.STOCK_SELL
        if price is not None and price <= 0:
            raise err(ErrorCode.PARAM_INVALID, f"限价必须为正，收到 {price}")
        price_type = xtconstant.FIX_PRICE if price is not None else xtconstant.LATEST_PRICE
        final_price = price if price is not None else 0.0
        remark = f"solidrock {order_remark}".strip()

        order_id = self._trader.order_stock(
            self._account,
            symbol,
            order_type,
            qty,
            price_type,
            final_price,
            strategy_name=strategy_name,
            order_remark=remark,
        )
        receipt = {
            "order_id": order_id,
            "accepted": order_id is not None and order_id != -1,
        }
        self._audit(
            {
                "action": "submit_order",
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "price": price,
                "price_type": "FIX" if price is not None else "LATEST",
                "order_id": order_id,
                "strategy_name": strategy_name,
                "remark": remark,
            }
        )
        if not receipt["accepted"]:
            raise err(
                ErrorCode.LIVE_ORDER_FAILED,
                f"实盘下单被拒绝：order_id={order_id}",
                hint="检查资金/持仓是否充足、价格是否越界；cfquant Web 控制台可查委托详情",
            )
        return receipt

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """撤销委托。"""
        self._connect()
        self._ensure_writable()
        result = self._trader.cancel_order_stock(self._account, order_id)
        self._audit({"action": "cancel_order", "order_id": order_id, "result": result})
        out: dict[str, Any] = {"order_id": order_id, "cancel_result": result}
        return out

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _wrap(obj: Any, kind: str) -> dict[str, Any]:
        """把 QMT 返回对象转为字典（兼容 dict / __dict__ / 属性式对象）。"""
        if isinstance(obj, dict):
            return dict(obj)
        result: dict[str, Any] = {}
        for name in dir(obj):
            if name.startswith("_"):
                continue
            try:
                value = getattr(obj, name)
            except Exception:
                continue
            if callable(value):
                continue
            result[name] = value
        return result or {"kind": kind, "repr": repr(obj)}

    def _audit(self, entry: dict[str, Any]) -> None:
        audit_dir = self.data_dir / "live"
        audit_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "account": self.account_id,
            **entry,
        }
        with (audit_dir / "audit.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
