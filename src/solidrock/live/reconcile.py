"""持仓对账：目标持仓 vs QMT 实际持仓的差异报告."""

from __future__ import annotations

from typing import Any


def diff_positions(
    target: dict[str, int],
    actual: dict[str, int],
) -> list[dict[str, Any]]:
    """对比目标持仓与实际持仓，返回需要执行的差异动作列表.

    - ``target``：目标持仓（symbol → 股数，0/缺失 = 清仓该标的）；
    - ``actual``：QMT 实际持仓（symbol → 可用股数）。

    返回动作列表：``{"action": "buy"/"sell", "symbol", "qty"}``；
    买入按 100 股整手取整，卖出按实际可用零股（清仓场景）。
    两边一致时返回空列表（已对平）。
    """
    actions: list[dict[str, Any]] = []
    all_symbols = sorted(set(target) | set(actual))
    for symbol in all_symbols:
        tgt = int(target.get(symbol, 0))
        act = int(actual.get(symbol, 0))
        delta = tgt - act
        if delta == 0:
            continue
        if delta > 0:
            qty = (delta // 100) * 100  # 买入整手
            if qty > 0:
                actions.append({"action": "buy", "symbol": symbol, "qty": qty})
        else:
            actions.append({"action": "sell", "symbol": symbol, "qty": -delta})
    return actions


def render_reconcile_markdown(actual: dict[str, int], target: dict[str, int], actions: list[dict[str, Any]]) -> str:
    """对账结果的 Markdown 报告."""
    lines = ["# 实盘对账报告", ""]
    lines.append("| 标的 | 实际持仓 | 目标持仓 | 差异 |")
    lines.append("|------|----------|----------|------|")
    for symbol in sorted(set(actual) | set(target)):
        act = actual.get(symbol, 0)
        tgt = target.get(symbol, 0)
        mark = "✓" if act == tgt else f"{tgt - act:+d}"
        lines.append(f"| {symbol} | {act} | {tgt} | {mark} |")
    lines.append("")
    if actions:
        lines.append("## 建议动作（未执行，仅供参考）")
        lines.append("")
        for a in actions:
            lines.append(f"- {a['action'].upper()} {a['symbol']} {a['qty']} 股")
        lines.append("")
        lines.append("> 对账报告只给出差异；是否执行由你决定（`srq live order` 显式下单）。")
    else:
        lines.append("实际持仓与目标一致，无需调仓。")
    return "\n".join(lines)
