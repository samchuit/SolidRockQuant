"""策略代码静态校验（AST 级）.

LLM 生成策略最常见的错误是**前视偏差**——用了未来数据。回测前先跑一遍
静态检查，拦截最低级的坑::

    close.shift(-1)      # 未来函数（错误：把明天的数据挪到今天用）
    close.bfill()        # 后向填充引入未来信息
    ctx.order_xxx(...)   # 不存在的 API（幻觉调用）

检查是启发式的（静态分析无法覆盖所有情况），定位为"低成本的第一道闸"，
不能替代引擎层面的防前视设计（next_open 执行模式）。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solidrock.agent.errors import ErrorCode, err

# Context 上允许调用的方法/属性（只读属性同样不允许赋值）
CTX_READONLY_ATTRS = {"now", "params", "portfolio", "universe"}
CTX_METHODS = {
    "order",
    "order_value",
    "order_target_percent",
    "order_target_value",
    "cancel_all",
    "history",
    "position",
    "log",
}
CTX_SETTABLE = {"universe"}

# 明确的未来函数模式
LOOKAHEAD_CALLS = {"bfill", "backfill"}


@dataclass
class Issue:
    severity: str  # error | warning
    code: str  # ErrorCode 值
    message: str
    line: int
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "line": self.line,
        }
        if self.hint:
            out["hint"] = self.hint
        return out


class _StrategyVisitor(ast.NodeVisitor):
    """收集 Strategy 子类并检查其方法体."""

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        self.strategy_classes: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if any(
            (isinstance(b, ast.Name) and "Strategy" in b.id) or (isinstance(b, ast.Attribute) and "Strategy" in b.attr)
            for b in node.bases
        ):
            self.strategy_classes.append(node.name)
            for child in node.body:
                self._check_node(child)
        self.generic_visit(node)

    def _check_node(self, node: ast.AST) -> None:
        for item in ast.walk(node):
            if isinstance(item, ast.Assign):
                # ctx.xxx = ... 赋值检查
                for target in item.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "ctx"
                        and target.attr not in CTX_SETTABLE
                    ):
                        self.issues.append(
                            Issue(
                                "error",
                                ErrorCode.STRATEGY_INVALID.value,
                                f"ctx.{target.attr} 是只读属性，不能赋值",
                                item.lineno,
                                hint=f"ctx 上可设置的只有 {sorted(CTX_SETTABLE)}（在 setup 中设置）",
                            )
                        )
            elif isinstance(item, ast.Call):
                func = item.func
                if not isinstance(func, ast.Attribute):
                    continue
                if (
                    isinstance(func.value, ast.Name)
                    and func.value.id == "ctx"
                    and func.attr not in CTX_METHODS | CTX_READONLY_ATTRS
                ):
                    self.issues.append(
                        Issue(
                            "error",
                            ErrorCode.STRATEGY_INVALID.value,
                            f"ctx.{func.attr} 不是可用的 Context API",
                            item.lineno,
                            hint=f"可用方法：{sorted(CTX_METHODS)}；只读属性：{sorted(CTX_READONLY_ATTRS)}",
                        )
                    )
                # shift(-n) 未来函数
                if func.attr == "shift" and _has_negative_arg(item):
                    self.issues.append(
                        Issue(
                            "error",
                            ErrorCode.LOOKAHEAD_SUSPECTED.value,
                            "shift(负数) 把未来数据挪到当前时点（前视偏差）",
                            item.lineno,
                            hint="策略只能使用截至当前 bar 的数据；如需'未来'参照，改为在后续 bar 里比较历史值",
                        )
                    )
                # bfill/backfill 引入未来信息
                if func.attr in LOOKAHEAD_CALLS:
                    self.issues.append(
                        Issue(
                            "warning",
                            ErrorCode.LOOKAHEAD_SUSPECTED.value,
                            f"{func.attr}() 用未来的值回填当前缺失值（潜在前视）",
                            item.lineno,
                            hint="改用 ffill（用过去值填充）",
                        )
                    )


def _has_negative_arg(call: ast.Call) -> bool:
    """shift(-n) 或 shift(periods=-n)。"""
    for arg in (*call.args, *(kw.value for kw in call.keywords if kw.arg == "periods")):
        if isinstance(arg, ast.Constant) and isinstance(arg.value, int) and arg.value < 0:
            return True
        if (
            isinstance(arg, ast.UnaryOp)
            and isinstance(arg.op, ast.USub)
            and isinstance(arg.operand, ast.Constant)
            and isinstance(arg.operand.value, (int, float))
        ):
            return True
    return False


def validate_strategy_file(path: str | Path) -> dict[str, Any]:
    """静态检查策略文件，返回 ``{strategy_classes, issues, passed}``（不抛错，问题进 issues）."""
    path = Path(path)
    issues: list[Issue] = []
    if not path.exists():
        raise err(
            ErrorCode.PARAM_INVALID,
            f"策略文件不存在：{path}",
            hint="传入策略 .py 文件的绝对路径",
        )
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        issues.append(
            Issue(
                "error",
                ErrorCode.STRATEGY_INVALID.value,
                f"语法错误：{exc.msg}",
                exc.lineno or 0,
                hint="先修复语法再回测",
            )
        )
        return {"strategy_classes": [], "issues": [i.to_dict() for i in issues], "passed": []}

    visitor = _StrategyVisitor(issues)
    visitor.visit(tree)
    if not visitor.strategy_classes:
        issues.append(
            Issue(
                "error",
                ErrorCode.STRATEGY_INVALID.value,
                "文件中没有 Strategy 子类",
                1,
                hint="class MyStrategy(Strategy): 并实现 setup/on_signal",
            )
        )

    passed = [
        "syntax",
        *([f"strategy_class:{name}" for name in visitor.strategy_classes]),
    ]
    if not any(i.code == ErrorCode.LOOKAHEAD_SUSPECTED.value for i in issues):
        passed.append("no_obvious_lookahead")
    if not any(i.code == ErrorCode.STRATEGY_INVALID.value for i in issues):
        passed.append("context_api_usage")
    return {
        "strategy_classes": visitor.strategy_classes,
        "issues": [i.to_dict() for i in issues],
        "passed": passed,
    }
