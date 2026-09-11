r"""持仓对账：目标持仓 vs QMT 实际持仓的差异报告.

**口径（重要）**：对比用**总持仓**（QMT ``volume``），卖出可行性用**可用数量**
（``can_use_volume``）。二者必须分开：A 股 T+1 下当日买入 ``volume=1000`` 但
``can_use_volume=0``，若拿可卖量当持仓去对账，会把"已持有"误判成"缺 1000 股"
并触发重复买入。

**安全默认**：``liquidate_untracked=False``——只出现在实盘、目标里完全没有的标的
**不会**被自动下卖单（由 :func:`untracked_positions` 单独报告为待确认项）。把
"目标里没有"等同于"要清仓"，在目标来自局部来源（如只交易少数标的的模拟盘）时会
误清掉实盘其他持仓。
"""

from __future__ import annotations

from typing import Any


def diff_positions(
    target: dict[str, int],
    actual: dict[str, int],
    *,
    available: dict[str, int] | None = None,
    liquidate_untracked: bool = False,
) -> list[dict[str, Any]]:
    """对比目标持仓与实际持仓，返回建议执行的差异动作列表.

    - ``target``：目标持仓（symbol → 股数）；显式写 0 表示清仓该标的；
    - ``actual``：QMT **总持仓**（symbol → ``volume``）；
    - ``available``：可卖数量（symbol → ``can_use_volume``）；None 时视为等于 ``actual``；
    - ``liquidate_untracked``：True 时才把"target 里完全没有的标的"当作清仓处理。

    返回动作列表：``{"action": "buy"/"sell"/"hold", "symbol", "qty", "note"?}``；
    买入按 100 股整手向下取整，卖出受可用数量约束（T+1 锁定部分不卖，附 note 说明）。
    两边一致时返回空列表（已对平）。
    """
    actions: list[dict[str, Any]] = []
    for symbol in sorted(target):
        tgt = int(target.get(symbol, 0))
        act = int(actual.get(symbol, 0))
        delta = tgt - act
        if delta == 0:
            continue
        if delta > 0:
            qty = (delta // 100) * 100  # 买入整手
            if qty > 0:
                actions.append({"action": "buy", "symbol": symbol, "qty": qty})
            continue
        # 卖出：只能卖可卖部分（T+1 锁定的当日买入不可卖）
        sellable = max(int((available if available is not None else actual).get(symbol, act)), 0)
        qty = min(-delta, sellable)
        if qty <= 0:
            actions.append(
                {
                    "action": "hold",
                    "symbol": symbol,
                    "qty": 0,
                    "note": f"应减 {-delta} 股但可卖数量为 0（T+1 锁定），今日无法调整",
                }
            )
            continue
        action: dict[str, Any] = {"action": "sell", "symbol": symbol, "qty": qty}
        if qty < -delta:
            action["note"] = f"应减 {-delta} 股，受可用数量限制仅卖 {qty} 股（其余 T+1 锁定）"
        actions.append(action)

    if liquidate_untracked:
        for item in untracked_positions(target, actual):
            symbol = item["symbol"]
            act = int(item["qty"])
            sellable = max(int((available if available is not None else actual).get(symbol, act)), 0)
            qty = min(act, sellable)
            if qty > 0:
                actions.append({"action": "sell", "symbol": symbol, "qty": qty, "note": "目标未包含（清仓）"})
    return actions


def untracked_positions(target: dict[str, int], actual: dict[str, int]) -> list[dict[str, Any]]:
    """实盘持有但目标未包含的标的（需人工确认，默认不自动清仓）."""
    return [
        {"symbol": symbol, "qty": int(actual.get(symbol, 0))}
        for symbol in sorted(set(actual) - set(target))
        if int(actual.get(symbol, 0)) > 0
    ]


def render_reconcile_markdown(
    actual: dict[str, int],
    target: dict[str, int],
    actions: list[dict[str, Any]],
    *,
    available: dict[str, int] | None = None,
    untracked: list[dict[str, Any]] | None = None,
) -> str:
    """对账结果的 Markdown 报告."""
    avail_map = available if available is not None else actual
    lines = ["# 实盘对账报告", ""]
    lines.append("| 标的 | 实际持仓 | 可卖 | 目标持仓 | 差异 |")
    lines.append("|------|----------|------|----------|------|")
    for symbol in sorted(set(actual) | set(target)):
        act = actual.get(symbol, 0)
        avail = avail_map.get(symbol, act)
        tgt = target.get(symbol, 0)
        mark = "✓" if act == tgt else f"{tgt - act:+d}"
        lines.append(f"| {symbol} | {act} | {avail} | {tgt} | {mark} |")
    lines.append("")

    if untracked:
        lines.append("## ⚠ 实盘持有但目标未包含（未自动清仓，请人工确认）")
        lines.append("")
        for item in untracked:
            lines.append(f"- {item['symbol']}：{item['qty']} 股")
        lines.append("")

    if actions:
        lines.append("## 建议动作（未执行，仅供参考）")
        lines.append("")
        for a in actions:
            label = str(a["action"]).upper()
            note = f" —— {a['note']}" if a.get("note") else ""
            lines.append(f"- {label} {a['symbol']} {a['qty']} 股{note}")
        lines.append("")
        lines.append("> 对账报告只给出差异；是否执行由你决定（`srq live order` 显式下单）。")
    else:
        lines.append("实际持仓与目标一致，无需调仓。")
    return "\n".join(lines)
