"""Finite DATA boundary around the unchanged original cost projection.

There is no Host, broker, billing, reservation, fallback, or authority substitute.
The shared codec is a hash-bound staged dependency, not a copied serializer.
"""

import sys

from ordered_json_v2 import NonFiniteJSONError, PROTOCOL, dumps, loads
from original_usage_cost import create_cost_operation

# The base manifest ID is not accepted by the pure function directly. The
# original AIUsageHostFactoryV4 routes all resolved cost IDs to "calculate".
# Preserve its base-ID mapping explicitly; retain meaningful pure aliases.
OPERATIONS = {
    "calculate": "calculate",
    "normalize": "normalize",
    "rumi_ai_usage_pack.ai-usage-cost": "calculate",
    "rumi_ai_usage_pack.ai-usage-cost.generate":
        "rumi_ai_usage_pack.ai-usage-cost.generate",
    "rumi_ai_usage_pack.ai-usage-cost.stream":
        "rumi_ai_usage_pack.ai-usage-cost.stream",
}


class ProviderInputError(ValueError):
    """Invalid operation identity or encoded caller DATA; no provider replay."""


class ProviderValidationFailure(RuntimeError):
    """Original provider validation failed, not invalid boundary input."""

    retryable = False


class ProviderArithmeticFailure(RuntimeError):
    """Original arithmetic overflowed or returned a nonfinite numeric result."""

    retryable = False


class ProviderOutputFailure(RuntimeError):
    """Successful provider output cannot cross the bounded DATA transport."""

    retryable = False


def invoke(operation: str, payload: str) -> str:
    """Project cost once, without manufacturing zero/unknown on arithmetic failure."""
    if type(operation) is not str or operation not in OPERATIONS:
        raise ProviderInputError("unknown usage cost operation")
    if sys.get_int_max_str_digits() != 4300:
        raise ProviderInputError("v2 requires the unchanged 4300-digit limit")
    try:
        value = loads(payload)
    except (ValueError, TypeError, OverflowError):
        raise ProviderInputError("outside ordered-finite-json-v2 domain") from None
    if type(value) is not dict:
        raise ProviderInputError("usage cost request must be an object")
    try:
        result = create_cost_operation(None)(OPERATIONS[operation], value)
    except OverflowError:
        raise ProviderArithmeticFailure("usage arithmetic overflow") from None
    except (TypeError, ValueError):
        raise ProviderValidationFailure("usage provider validation failed") from None
    try:
        return dumps(result)
    except (NonFiniteJSONError, OverflowError):
        raise ProviderArithmeticFailure("nonfinite usage result") from None
    except (TypeError, ValueError):
        raise ProviderOutputFailure("usage result outside DATA domain") from None
