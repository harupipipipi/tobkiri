"""Calculator admission and bounded arithmetic without external operations."""

from types import SimpleNamespace

import pytest

from ecosystem.rumi_default_tools_pack.domain.tool import calculator
from ecosystem.rumi_default_tools_pack.runtime.calculator import _bind


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("6 * 7", 42),
        ("-(3 + 4)", -7),
        ("+4 / 2", 2.0),
        ("10 // 3", 3),
        ("10 % 3", 1),
        ("2 ** -2", 0.25),
        ("1.5 + 0.25", 1.75),
        (" 2 + 3 ", 5),
        ("2 ** 100", 2**100),
    ],
)
def test_finite_arithmetic(expression: str, expected: int | float) -> None:
    assert calculator.calculate(expression) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "",
        " ",
        "True",
        "False",
        "None",
        "'text'",
        "[1]",
        "(1, 2)",
        "__import__('os').getcwd()",
        "a",
        "(1).real",
        "1 << 4",
        "1 / 0",
        "1e309",
        "1e308 * 10",
        "(-1) ** 0.5",
        "2 ** 101",
        "2 ** -101",
        "((2 ** 100) ** 100) ** 100",
        "1+" * 65 + "1",
        "-" * 34 + "1",
        "1" * 2049,
        "１+２",
        None,
        True,
        42,
        {"approved": True},
    ],
)
def test_unsupported_or_excessive_work_is_rejected(expression: object) -> None:
    with pytest.raises(ValueError):
        calculator.calculate(expression)  # type: ignore[arg-type]


def test_large_power_is_rejected_before_allocating_its_result(monkeypatch) -> None:
    calls = []
    original = calculator._BINARY[calculator.ast.Pow]

    def power(left, right):
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setitem(calculator._BINARY, calculator.ast.Pow, power)
    with pytest.raises(ValueError, match="power is too large"):
        calculator.calculate("(2 ** 100) ** 100")
    assert calls == [(2, 100)]


def test_host_handler_only_accepts_its_own_tool_and_argument_shape() -> None:
    invoke = _bind(None)  # type: ignore[arg-type]
    context = SimpleNamespace(assert_current=lambda: None)
    payload = {
        "tool_id": "calculator",
        "tool_call_id": "call-1",
        "arguments": {"expression": "6*7"},
    }
    assert invoke(payload, context) == {  # type: ignore[arg-type]
        "result": "Calculated: 6*7 = 42",
        "is_error": False,
        "widget": None,
    }
    for change in (
        {"tool_id": "file_read"},
        {"approved": True},
        {"profile_id": "other"},
        {"tool_call_id": ""},
        {"arguments": {"expression": "6*7", "path": "/tmp"}},
    ):
        with pytest.raises(ValueError, match="invocation payload"):
            invoke({**payload, **change}, context)  # type: ignore[arg-type]


def test_cancelled_invocation_never_evaluates(monkeypatch) -> None:
    from ecosystem.rumi_default_tools_pack.runtime import calculator as host

    def cancelled():
        raise PermissionError("cancelled")

    monkeypatch.setattr(host, "calculate", lambda _: pytest.fail("evaluated after cancel"))
    with pytest.raises(PermissionError, match="cancelled"):
        host._bind(None)(  # type: ignore[arg-type]
            {"tool_id": "calculator", "tool_call_id": "call-1", "arguments": {"expression": "6*7"}},
            SimpleNamespace(assert_current=cancelled),
        )
