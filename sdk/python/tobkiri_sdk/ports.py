"""Generic gated Pack port calls; shape names are supplied by the Pack."""

import json
from typing import Any
from ._binding import invoke


def call(shape: str, operation: str, arguments: Any) -> Any:
    """Call an explicitly named port through core's principal-aware proxy."""
    value = invoke("flow", "call-port", shape=shape, op=operation,
                   args=json.dumps(arguments, ensure_ascii=False, allow_nan=False))
    return json.loads(value)


def call_stream(shape: str, operation: str, arguments: Any) -> Any:
    """Return an opaque core stream handle from a gated port call."""
    return invoke("flow", "call-port-stream", shape=shape, op=operation,
                  args=json.dumps(arguments, ensure_ascii=False, allow_nan=False))
