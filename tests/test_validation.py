"""策略静态校验测试."""

from __future__ import annotations

from pathlib import Path

import pytest

from solidrock.agent.errors import ErrorCode, SolidRockError
from solidrock.agent.validation import validate_strategy_file

GOOD_STRATEGY = """
from solidrock import Context, Strategy

class Good(Strategy):
    params = {"fast": 5}

    def setup(self, ctx: Context) -> None:
        ctx.universe = ["510300.SH"]

    def on_signal(self, ctx: Context) -> None:
        close = ctx.history("510300.SH", 10, fields="close")["510300.SH"]
        if close.iloc[-1] > close.iloc[-int(self.params["fast"]):].mean():
            ctx.order_target_percent("510300.SH", 1.0)
"""

LOOKAHEAD_STRATEGY = """
from solidrock import Strategy

class Lookahead(Strategy):
    def setup(self, ctx) -> None:
        ctx.universe = ["510300.SH"]

    def on_signal(self, ctx) -> None:
        close = ctx.history("510300.SH", 10, fields="close")["510300.SH"]
        tomorrow = close.shift(-1)          # 未来函数
        fixed = close.bfill()               # 后向填充
        if tomorrow.iloc[-1] > fixed.iloc[-1]:
            ctx.order("510300.SH", 100)
"""

HALLUCINATED_API = """
from solidrock import Strategy

class Hallucinated(Strategy):
    def setup(self, ctx) -> None:
        ctx.universe = ["510300.SH"]
        ctx.risk_limits = {"max": 0.5}      # 只读属性赋值

    def on_signal(self, ctx) -> None:
        ctx.buy_stock("510300.SH", 100)     # 幻觉方法
        ctx.set_param("fast", 5)            # 幻觉方法
"""


def _write(tmp_path: Path, code: str) -> Path:
    p = tmp_path / "strategy_under_test.py"
    p.write_text(code, encoding="utf-8")
    return p


class TestValidate:
    def test_good_strategy_passes(self, tmp_path: Path) -> None:
        report = validate_strategy_file(_write(tmp_path, GOOD_STRATEGY))
        assert report["strategy_classes"] == ["Good"]
        assert report["issues"] == []
        assert "no_obvious_lookahead" in report["passed"]
        assert "context_api_usage" in report["passed"]

    def test_lookahead_detected(self, tmp_path: Path) -> None:
        report = validate_strategy_file(_write(tmp_path, LOOKAHEAD_STRATEGY))
        codes = [(i["code"], i["severity"]) for i in report["issues"]]
        assert ("LOOKAHEAD_SUSPECTED", "error") in codes  # shift(-1)
        assert ("LOOKAHEAD_SUSPECTED", "warning") in codes  # bfill
        issues = [i for i in report["issues"] if i["code"] == "LOOKAHEAD_SUSPECTED"]
        assert all(i["line"] > 0 for i in issues)  # 带行号定位

    def test_hallucinated_api_detected(self, tmp_path: Path) -> None:
        report = validate_strategy_file(_write(tmp_path, HALLUCINATED_API))
        messages = [i["message"] for i in report["issues"]]
        assert any("ctx.buy_stock" in m for m in messages)
        assert any("ctx.set_param" in m for m in messages)
        assert any("ctx.risk_limits" in m for m in messages)
        assert all(i["severity"] == "error" for i in report["issues"])

    def test_syntax_error(self, tmp_path: Path) -> None:
        report = validate_strategy_file(_write(tmp_path, "def broken(:\n"))
        assert report["strategy_classes"] == []
        assert report["issues"][0]["severity"] == "error"
        assert "语法" in report["issues"][0]["message"]

    def test_no_strategy_class(self, tmp_path: Path) -> None:
        report = validate_strategy_file(_write(tmp_path, "x = 1\n"))
        assert any("没有 Strategy 子类" in i["message"] for i in report["issues"])

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SolidRockError) as exc_info:
            validate_strategy_file(tmp_path / "nope.py")
        assert exc_info.value.code is ErrorCode.PARAM_INVALID
