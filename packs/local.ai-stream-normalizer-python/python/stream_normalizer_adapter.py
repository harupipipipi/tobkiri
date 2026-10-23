"""Finite DATA boundary around the unchanged original stream normalizer.

The original projects events in order and validates the terminal event. This
adapter neither adds a terminal event nor retries, rolls back, or repairs data.
"""

import sys

from ordered_json_v2 import NonFiniteJSONError, PROTOCOL, dumps, loads
from original_stream_normalizer import (
    GlobalContractInvocationError,
    create_stream_normalize_operation,
)

OPERATIONS = {
    "normalize": "normalize",
    "validate": "validate",
    "rumi_ai_stream_pack.ai-stream-normalize":
        "rumi_ai_stream_pack.ai-stream-normalize",
}


class ProviderInputError(ValueError):
    """Invalid operation identity or encoded caller DATA; no provider replay."""


class ProviderValidationFailure(RuntimeError):
    """Original event or binding validation failed, not input codec validation."""

    retryable = False


class ProviderArithmeticFailure(RuntimeError):
    """Original numeric conversion overflowed or returned nonfinite output."""

    retryable = False


class ProviderOutputFailure(RuntimeError):
    """Successful provider output cannot cross the bounded DATA transport."""

    retryable = False


def invoke(operation: str, payload: str) -> str:
    """Normalize once; keep provider validation and typed input errors distinct."""
    if type(operation) is not str or operation not in OPERATIONS:
        raise ProviderInputError("unknown stream normalization operation")
    if sys.get_int_max_str_digits() != 4300:
        raise ProviderInputError("v2 requires the unchanged 4300-digit limit")
    try:
        value = loads(payload)
    except (ValueError, TypeError, OverflowError):
        raise ProviderInputError("outside ordered-finite-json-v2 domain") from None
    if type(value) is not dict:
        raise ProviderInputError("stream normalization request must be an object")
    try:
        result = create_stream_normalize_operation(None)(OPERATIONS[operation], value)
    except OverflowError:
        raise ProviderArithmeticFailure("stream numeric overflow") from None
    except (GlobalContractInvocationError, TypeError, ValueError):
        raise ProviderValidationFailure("stream provider validation failed") from None
    try:
        return dumps(result)
    except (NonFiniteJSONError, OverflowError):
        raise ProviderArithmeticFailure("nonfinite stream result") from None
    except (TypeError, ValueError):
        raise ProviderOutputFailure("stream result outside DATA domain") from None
