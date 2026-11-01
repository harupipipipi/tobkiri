"""Additive source candidate, not the preserved v1 component's entry point."""

import sys

from caller_adapter import OPERATIONS
import ordered_json_v2 as codec
from ordered_json_v2 import PROTOCOL, dumps, loads
from original_bridge import create_tool_intent_operation


class NonFiniteProviderOutputError(RuntimeError):
    """Terminal source-provider failure, not invalid caller input or Core data."""

    code = "provider_failed"
    retryable = False

    def __init__(self) -> None:
        super().__init__("provider output contains a non-finite number")


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
    # A successful original call can produce NaN/Infinity from opaque argument
    # JSON. The old audited Host rejects that output as provider failure. Keep
    # this distinct from request/transport errors without exporting nonfinite
    # data, replaying normalization, or importing the legacy Host.
    try:
        return dumps(output)
    except codec.NonFiniteJSONError:
        raise NonFiniteProviderOutputError() from None


def response(payload: str) -> object:
    """Decode successful v2 output without coercing or tagging its integers."""
    value = loads(payload)
    if type(value) is not dict or set(value) != {"intents"}:
        raise ValueError("invalid ordered tool bridge response")
    return value
