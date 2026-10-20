"""Additive source candidate, not the preserved v1 component's entry point."""

import sys

from caller_adapter import OPERATIONS
from ordered_json_v2 import PROTOCOL, dumps, loads
from original_bridge import create_tool_intent_operation


def request(payload: object) -> str:
    """Encode an exact native request in the bounded v2 domain."""
    return dumps(payload)


def invoke(operation: str, payload: str) -> str:
    """Normalize through the unchanged original, without granting authority."""
    if operation not in OPERATIONS:
        raise ValueError("unknown ordered tool bridge operation")
    if sys.get_int_max_str_digits() != 4300:
        raise ValueError("v2 requires the unchanged 4300-digit interpreter limit")
    value = loads(payload)
    if type(value) is not dict:
        raise ValueError("tool bridge request must be an object")
    output = create_tool_intent_operation(None)("normalize", value)
    return dumps(output)


def response(payload: str) -> object:
    """Decode successful v2 output without coercing or tagging its integers."""
    value = loads(payload)
    if type(value) is not dict or set(value) != {"intents"}:
        raise ValueError("invalid ordered tool bridge response")
    return value
