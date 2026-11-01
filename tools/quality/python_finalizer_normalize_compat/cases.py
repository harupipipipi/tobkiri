"""Hand-authored usage inputs for the original finalizer normalizer probe."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UsageCase:
    """One replacement for, or omission of, the fixture's value.usage field."""

    case_id: str
    usage_present: bool
    usage: Any = None


def usage_cases() -> tuple[UsageCase, ...]:
    """Return inputs only; every observed result is produced by CPython."""
    return (
        UsageCase("usage-missing", False),
        UsageCase("usage-null", True, None),
        UsageCase("usage-false", True, False),
        UsageCase("usage-zero-int", True, 0),
        UsageCase("usage-zero-float", True, 0.0),
        UsageCase("usage-empty-string", True, ""),
        UsageCase("usage-empty-list", True, []),
        UsageCase("usage-empty-object", True, {}),
        UsageCase("usage-string-ab", True, "ab"),
        UsageCase("usage-string-pair-row", True, ["ab"]),
        UsageCase("usage-nonbmp-string-pair-row", True, ["😀💥"]),
        UsageCase("usage-object-pair-row-z-a", True, [{"z": 1, "a": 2}]),
        UsageCase("usage-object-pair-row-a-z", True, [{"a": 2, "z": 1}]),
        UsageCase(
            "usage-array-pairs",
            True,
            [["input_tokens", 2], ["output_tokens", 3]],
        ),
        UsageCase(
            "usage-hashable-nonstring-keys",
            True,
            [[True, "bool"], [1, "int"], [1.0, "float"], [None, "none"]],
        ),
        UsageCase(
            "usage-duplicate-pairs-order",
            True,
            [["z", 1], ["a", 2], ["z", 3], ["m", 4]],
        ),
        UsageCase(
            "usage-object-order-z-a",
            True,
            {"z": 1, "a": 2},
        ),
        UsageCase(
            "usage-object-order-a-z",
            True,
            {"a": 2, "z": 1},
        ),
        UsageCase("usage-pair-row-too-short", True, [["x"]]),
        UsageCase("usage-pair-row-too-long", True, [["x", 1, 2]]),
        UsageCase("usage-nonstring-then-invalid-row", True, [[1, "first"], ["x"]]),
        UsageCase("usage-nonstring-then-noniterable-row", True, [[1, "first"], 2]),
        UsageCase("usage-unhashable-list-key", True, [[["x"], 1]]),
        UsageCase("usage-unhashable-object-key", True, [[{"x": 1}, 1]]),
    )
