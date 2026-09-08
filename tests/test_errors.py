"""错误信封规范测试（MCP 输出契约的一部分）."""

from __future__ import annotations

from solidrock.agent.errors import ErrorCode, SolidRockError, err


def test_err_builder() -> None:
    e = err(ErrorCode.NO_DATA, "本地没有数据", hint="先执行 srq data update")
    assert e.code is ErrorCode.NO_DATA
    assert e.message == "本地没有数据"
    assert e.hint == "先执行 srq data update"


def test_to_dict_envelope() -> None:
    e = err(
        ErrorCode.SYMBOL_NOT_FOUND,
        "代码不存在",
        hint="调用 search_instruments",
        details={"raw": "1"},
    )
    d = e.to_dict()
    assert d == {
        "code": "SYMBOL_NOT_FOUND",
        "message": "代码不存在",
        "hint": "调用 search_instruments",
        "details": {"raw": "1"},
    }


def test_to_dict_omits_missing_fields() -> None:
    assert err(ErrorCode.NO_DATA, "x").to_dict() == {"code": "NO_DATA", "message": "x"}


def test_str_contains_code_and_hint() -> None:
    text = str(err(ErrorCode.NO_DATA, "没有数据", hint="跑一下更新"))
    assert "[NO_DATA]" in text
    assert "hint: 跑一下更新" in text


def test_is_exception() -> None:
    assert issubclass(SolidRockError, Exception)
