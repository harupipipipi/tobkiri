"""External-QA-oriented specifications for token and usage-cost contracts."""

from __future__ import annotations

from ecosystem.rumi_ai_usage_pack.runtime.usage import (
    create_cost_operation,
    create_tokenize_operation,
)


def test_token_count_is_explicitly_an_estimate() -> None:
    result = create_tokenize_operation(None)("estimate", {"input": "hello"})

    assert result["tokens"] > 0
    assert result["exact"] is False
    assert result["provenance"] == "deterministic_estimate"


def test_cost_preserves_unknown_and_calculates_known_usage() -> None:
    operation = create_cost_operation(None)
    unknown = operation("calculate", {"usage": {}, "pricing": {}})
    known = operation(
        "calculate",
        {
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "pricing": {"input": 0.1, "output": 0.2, "currency": "USD"},
            "pricing_revision": "fixture",
        },
    )

    assert unknown["known"] is False
    assert unknown["cost"] is None
    assert known["known"] is True
    assert known["cost"] == 2.0

