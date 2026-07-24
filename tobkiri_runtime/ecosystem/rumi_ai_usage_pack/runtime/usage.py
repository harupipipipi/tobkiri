"""Explicit estimated token counts and provider-reported usage cost."""

from __future__ import annotations

import json
import math
from typing import Any, Mapping


def create_tokenize_operation(client: Any):
    """Create a deterministic tokenizer-estimate contract."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"estimate", "count"}:
            raise ValueError(f"unknown tokenizer operation: {name}")
        value = payload.get("input", payload.get("messages", ""))
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "tokens": max(1, math.ceil(len(encoded) / 4)),
            "provenance": "deterministic_estimate",
            "exact": False,
        }

    return operation


def create_cost_operation(client: Any):
    """Create a cost calculator that preserves unknown values."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"calculate", "normalize"}:
            raise ValueError(f"unknown usage cost operation: {name}")
        usage = payload.get("usage")
        pricing = payload.get("pricing")
        usage = usage if isinstance(usage, Mapping) else {}
        pricing = pricing if isinstance(pricing, Mapping) else {}
        input_tokens = _number(
            usage.get("input_tokens", usage.get("prompt_tokens"))
        )
        output_tokens = _number(
            usage.get("output_tokens", usage.get("completion_tokens"))
        )
        input_rate = _number(pricing.get("input"))
        output_rate = _number(pricing.get("output"))
        known = None not in {
            input_tokens,
            output_tokens,
            input_rate,
            output_rate,
        }
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": (
                input_tokens + output_tokens
                if input_tokens is not None and output_tokens is not None
                else None
            ),
            "cost": (
                input_tokens * input_rate + output_tokens * output_rate
                if known else None
            ),
            "currency": str(pricing.get("currency") or "USD") if known else None,
            "known": known,
            "usage_provenance": str(
                payload.get("usage_provenance") or "provider_reported"
            ),
            "pricing_revision": payload.get("pricing_revision"),
        }

    return operation


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None

