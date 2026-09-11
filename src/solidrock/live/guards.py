r"""实盘下单守卫（Guard）：在 broker 之上叠加不可绕过的安全闸门.

为什么需要：Agent（LLM）能通过 MCP 直接调用 ``live_submit_order``，**一次调用
即产生真实委托**。"请在提示词里二次确认"不是控制手段，控制必须在代码层：

- **标的准入**：``SOLIDROCK_LIVE_SYMBOL_WHITELIST`` 非空时只允许白名单标的，
  杜绝代码笔误打进另一只合法标的；
- **金额上限**：单笔名义金额（``qty × 参考价``）不超过
  ``SOLIDROCK_LIVE_MAX_ORDER_NOTIONAL``（默认 50 万），拦住数量级笔误；
- **交易时段**：默认开启，非交易日或非 A 股交易时段拒绝下单；
- **幂等去重**：相同指纹（标的方向数量价格）在
  ``SOLIDROCK_LIVE_DUPLICATE_WINDOW_SECONDS``（默认 60s）内重复提交直接拒绝，
  防止 LLM 超时重试造成重复委托。

守卫由 :class:`~solidrock.live.broker.CfquantBroker` 在下单路径上**无条件下调用**，
调用方无法通过传参跳过；只读守卫（``LIVE_READ_ONLY``）仍然是最外层的第一道闸。

撤单不受守卫限制——撤单是降低风险的动作，任何时段都应放行。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from solidrock.agent.errors import ErrorCode, err

if TYPE_CHECKING:
    from solidrock.config import Settings

# A 股交易时段（集合竞价开始 ~ 收盘），分钟为单位
_SESSIONS_MIN: tuple[tuple[int, int], ...] = ((9 * 60 + 15, 11 * 60 + 30), (13 * 60, 15 * 60 + 5))

# 台账保留的最近记录条数（读取去重窗口时的扫描上限）
_LEDGER_SCAN = 500


def _fmt_notional(value: float) -> str:
    return f"{value:,.0f} 元"


class LiveOrderGuard:
    """下单前的安全闸门（纯读配置 + 本地数据 + 落盘台账，无网络依赖）."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        data_dir: str | Path | None = None,
        clock: Callable[[], pd.Timestamp] | None = None,
        calendar: Any | None = None,
    ) -> None:
        """``clock``/``calendar`` 供测试注入；``calendar`` 为 None 时交易日退化为
        "周一~周五"判断（无日历也可用，只是不含法定假日）。"""
        from solidrock.config import get_settings

        self.settings = settings or get_settings()
        self.data_dir = Path(data_dir) if data_dir else self.settings.resolved_data_dir()
        self._clock = clock or pd.Timestamp.now
        self._calendar = calendar
        self._ref_cache: dict[str, float] = {}

    # ------------------------------------------------------------------ 对外
    def check(self, symbol: str, side: str, qty: int, *, price: float | None = None) -> dict[str, Any]:
        """校验一笔待提交的订单；不通过则抛 ``SolidRockError``.

        通过时返回审计上下文（``ref_price`` / ``notional``），供 broker 写审计日志。
        校验顺序：白名单 → 交易时段 → 金额上限 → 幂等去重。
        """
        symbol = str(symbol).upper()
        self._check_whitelist(symbol)
        self._check_trading_hours()
        ref_price = self._resolve_ref_price(symbol, price)
        notional = self._check_notional(symbol, qty, price, ref_price)
        self._check_duplicate(symbol, side, qty, price)
        return {"ref_price": ref_price, "notional": notional}

    def record(self, symbol: str, side: str, qty: int, *, price: float | None = None) -> None:
        """把已提交订单写入台账（幂等去重的依据）."""
        if self._window_seconds() <= 0:
            return
        now = self._clock()
        entry = {
            "ts": now.isoformat(),
            "ts_epoch": float(now.timestamp()),
            "fingerprint": self._fingerprint(symbol, side, qty, price),
            "symbol": str(symbol).upper(),
            "side": side,
            "qty": int(qty),
            "price": price,
        }
        path = self._ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------ 检查项
    def _check_whitelist(self, symbol: str) -> None:
        allowed = self._whitelist()
        if not allowed or symbol in allowed:
            return
        raise err(
            ErrorCode.LIVE_SYMBOL_NOT_ALLOWED,
            f"标的 {symbol} 不在实盘白名单内",
            hint="白名单由 SOLIDROCK_LIVE_SYMBOL_WHITELIST 配置（逗号分隔）；"
            "确认无误后把该标的加入白名单，或清空该配置以解除限制",
            details={"symbol": symbol, "whitelist": sorted(allowed)},
        )

    def _check_trading_hours(self) -> None:
        if not self.settings.live_enforce_trading_hours:
            return
        now = self._clock()
        if not self._is_trading_day(now):
            raise err(
                ErrorCode.LIVE_NOT_TRADING_HOURS,
                f"{now.date()} 不是交易日，拒绝提交实盘订单",
                hint="如需在非交易日准备委托，设置 SOLIDROCK_LIVE_ENFORCE_TRADING_HOURS=false 后重试",
                details={"now": now.isoformat()},
            )
        minute = now.hour * 60 + now.minute
        if not any(start <= minute <= end for start, end in _SESSIONS_MIN):
            raise err(
                ErrorCode.LIVE_NOT_TRADING_HOURS,
                f"{now.strftime('%H:%M')} 不在 A 股交易时段（09:15-11:30 / 13:00-15:05）",
                hint="如需盘前盘后挂单，设置 SOLIDROCK_LIVE_ENFORCE_TRADING_HOURS=false 后重试",
                details={"now": now.isoformat()},
            )

    def _check_notional(self, symbol: str, qty: int, price: float | None, ref_price: float | None) -> float | None:
        cap = float(self.settings.live_max_order_notional or 0.0)
        if cap <= 0:
            return None
        if ref_price is None:
            raise err(
                ErrorCode.LIVE_ORDER_TOO_LARGE,
                f"无法确定 {symbol} 的参考价，无法校验单笔金额上限",
                hint="传限价（price）或先 srq data update 更新该标的日线数据；"
                "确需关闭校验可设 SOLIDROCK_LIVE_MAX_ORDER_NOTIONAL=0",
                details={"symbol": symbol},
            )
        notional = float(qty) * float(ref_price)
        if notional <= cap:
            return notional
        basis = "限价" if price is not None else f"最近收盘 {ref_price}"
        raise err(
            ErrorCode.LIVE_ORDER_TOO_LARGE,
            f"单笔名义金额 {_fmt_notional(notional)}（{qty} 股 × {basis}）超过上限 {_fmt_notional(cap)}",
            hint="拆分为多笔、降低数量，或调整 SOLIDROCK_LIVE_MAX_ORDER_NOTIONAL 上限",
            details={"symbol": symbol, "qty": int(qty), "ref_price": ref_price, "notional": notional, "cap": cap},
        )

    def _check_duplicate(self, symbol: str, side: str, qty: int, price: float | None) -> None:
        window = self._window_seconds()
        if window <= 0:
            return
        fp = self._fingerprint(symbol, side, qty, price)
        cutoff = float(self._clock().timestamp()) - window
        for rec in self._recent_entries():
            if rec.get("fingerprint") == fp and float(rec.get("ts_epoch", 0.0)) >= cutoff:
                raise err(
                    ErrorCode.LIVE_DUPLICATE_ORDER,
                    f"{window:.0f}s 内已提交过相同订单（{symbol} {side} {qty}），疑似重复委托",
                    hint="确认确实需要重复下单时，等待去重窗口过去后重试；"
                    "或设 SOLIDROCK_LIVE_DUPLICATE_WINDOW_SECONDS=0 关闭去重",
                    details={"symbol": symbol, "side": side, "qty": int(qty), "window_seconds": window},
                )

    # ------------------------------------------------------------------ 工具
    def _whitelist(self) -> set[str]:
        raw = self.settings.live_symbol_whitelist
        if not raw:
            return set()
        return {part.strip().upper() for part in str(raw).split(",") if part.strip()}

    def _window_seconds(self) -> float:
        return float(self.settings.live_duplicate_window_seconds or 0)

    def _is_trading_day(self, now: pd.Timestamp) -> bool:
        if self._calendar is not None:
            try:
                return bool(self._calendar.is_trading_day(now.normalize()))
            except Exception:  # 日历未加载等：退化为工作日判断，不因基础设施缺失而放行风险
                pass
        return now.weekday() < 5

    def _resolve_ref_price(self, symbol: str, price: float | None) -> float | None:
        """参考价：限价优先；否则取本地日线最近一根收盘（带进程内缓存）."""
        if price is not None and float(price) > 0:
            return float(price)
        if symbol in self._ref_cache:
            return self._ref_cache[symbol]
        resolved: float | None = None
        try:
            from solidrock.data.store import DataStore

            bars = DataStore(self.data_dir).load_bars([symbol], columns=["date", "close"])
            if bars is not None and not bars.empty:
                close = bars.sort_values("date")["close"].dropna()
                if not close.empty:
                    resolved = float(close.iloc[-1])
        except Exception:  # 数据缺失/损坏不应变成"放行"，故此处静默退化为 None 由上层拦截
            resolved = None
        if resolved is not None:
            self._ref_cache[symbol] = resolved
        return resolved

    def _ledger_path(self) -> Path:
        return self.data_dir / "live" / "order_intents.jsonl"

    def _recent_entries(self) -> list[dict[str, Any]]:
        path = self._ledger_path()
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-_LEDGER_SCAN:]
        except OSError:
            return []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    @staticmethod
    def _fingerprint(symbol: str, side: str, qty: int, price: float | None) -> str:
        px = "" if price is None else f"{float(price):.4f}"
        return f"{str(symbol).upper()}|{side}|{int(qty)}|{px}"
