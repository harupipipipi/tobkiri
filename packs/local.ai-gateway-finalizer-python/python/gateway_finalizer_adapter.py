"""Bounded DATA transport around source-extracted Gateway finalization.

The three required generated imports are the only dependencies. Candidate,
provider results and attempts are recorded DATA, not authenticated selection,
budget ownership or execution authority. Nothing retries or issues generation.
"""

from dataclasses import dataclass
import sys
from typing import Any

from componentize_py_types import Err
from wit_world.imports import caller, stream_normalizer, usage_cost

from ordered_json_v2 import dumps, loads
from original_gateway_finalizer import (
    Candidate,
    GlobalContractInvocationError,
    STREAM_NORMALIZE_CONTRACT,
    STREAM_NORMALIZE_OPERATION,
    USAGE_CONTRACT,
    USAGE_GENERATE_OPERATION,
    USAGE_STREAM_OPERATION,
    _finalize_generate_result,
    _finalize_stream_result,
)

PROTOCOL = "ordered-finite-json-v2"
OPERATIONS = ("generate", "stream")


class FinalizerInputError(ValueError):
    """Invalid boundary DATA, never an instruction to invoke a provider."""


class TerminalDependencyFailure(RuntimeError):
    """Not an original Gateway domain error; cannot enter its retry path."""

    retryable = False


class ImportedDependencyFailure(TerminalDependencyFailure):
    """Retain every imported Core error record for unchanged outer rethrow."""

    def __init__(self, core_error: Any) -> None:
        super().__init__("imported finalization dependency failed")
        self.core_error = core_error


class DependencyTransportFailure(TerminalDependencyFailure):
    """Encoding or generated import failed, without retry or fallback."""


class DependencyResponseFailure(TerminalDependencyFailure):
    """A successful dependency reply is unusable, never a domain rejection."""


class FinalizerProjectionFailure(TerminalDependencyFailure):
    """Original Python projection or output transport failed."""


@dataclass(frozen=True)
class DomainRejection:
    """Original domain code/message, distinct from the outer Core result error."""

    code: str
    message: str


def _exchange(function: Any, *arguments: str, payload: object) -> Any:
    try:
        encoded = dumps(payload)
    except (RecursionError, MemoryError):
        raise
    except Exception:
        raise DependencyTransportFailure("dependency DATA encoding failed") from None
    try:
        response = function(*arguments, encoded)
    except Err as error:
        raise ImportedDependencyFailure(error.value) from None
    except (RecursionError, MemoryError):
        raise
    except Exception:
        raise DependencyTransportFailure("dependency import failed") from None
    try:
        return loads(response)
    except (RecursionError, MemoryError):
        raise
    except Exception:
        raise DependencyResponseFailure("dependency DATA decoding failed") from None


class ImportedDependencies:
    """Narrow transport adapter, not a Host or GlobalContractClient substitute."""

    def invoke(self, contract: str, operation: str, payload: object) -> Any:
        """Route only the original usage and stream-normalization identities."""
        if (type(contract) is str and contract == USAGE_CONTRACT
                and type(operation) is str
                and operation in (USAGE_GENERATE_OPERATION, USAGE_STREAM_OPERATION)):
            result = _exchange(usage_cost.calculate, operation, payload=payload)
            if type(result) is not dict:
                raise DependencyResponseFailure("usage result must be an object")
            return result
        if (type(contract) is str and contract == STREAM_NORMALIZE_CONTRACT
                and type(operation) is str and operation == STREAM_NORMALIZE_OPERATION):
            result = _exchange(stream_normalizer.normalize, operation, payload=payload)
            if (type(result) is not dict or type(result.get("events")) is not list
                    or any(type(event) is not dict for event in result["events"])):
                raise DependencyResponseFailure("stream result must contain objects")
            return result
        raise FinalizerInputError("unsupported finalization dependency")

    def tool_intents(
        self, intents: list[Any], request_id: str, *, streaming: bool,
    ) -> list[dict[str, Any]]:
        """Call the original tool caller; do not repeat its local normalization."""
        if type(streaming) is not bool:
            raise FinalizerInputError("invalid tool caller selection")
        function = caller.stream_intents if streaming else caller.generate_intents
        result = _exchange(function, payload={
            "request_id": request_id, "intents": intents,
        })
        if type(result) is not list or any(type(item) is not dict for item in result):
            raise DependencyResponseFailure("tool caller result must contain objects")
        return result


def _candidate(value: Any) -> Candidate:
    fields = {
        "model_id", "provider_instance_id", "catalog_provider_instance_id",
        "catalog_revision", "capabilities", "modalities", "context_length",
        "input_cost", "output_cost", "priority", "available", "health",
        "health_observed_at", "raw",
    }
    if type(value) is not dict or set(value) != fields:
        raise FinalizerInputError("invalid recorded Candidate fields")
    for name in ("model_id", "provider_instance_id", "catalog_provider_instance_id",
                 "catalog_revision", "health"):
        if type(value[name]) is not str:
            raise FinalizerInputError("invalid recorded Candidate string")
    for name in ("capabilities", "modalities"):
        if (type(value[name]) is not list
                or any(type(item) is not str for item in value[name])):
            raise FinalizerInputError("invalid recorded Candidate set")
    for name in ("context_length", "priority"):
        if type(value[name]) is not int:
            raise FinalizerInputError("invalid recorded Candidate integer")
    for name in ("input_cost", "output_cost", "health_observed_at"):
        if value[name] is not None and type(value[name]) not in (int, float):
            raise FinalizerInputError("invalid recorded Candidate number")
    if type(value["available"]) is not bool or type(value["raw"]) is not dict:
        raise FinalizerInputError("invalid recorded Candidate metadata")
    return Candidate(**{
        **value, "capabilities": frozenset(value["capabilities"]),
        "modalities": frozenset(value["modalities"]),
    })


def _input(operation: str, payload: str) -> tuple[dict[str, Any], Candidate]:
    if type(operation) is not str or operation not in OPERATIONS:
        raise FinalizerInputError("unknown finalization operation")
    if sys.get_int_max_str_digits() != 4300:
        raise FinalizerInputError("v2 requires the unchanged 4300-digit limit")
    try:
        value = loads(payload)
    except (ValueError, TypeError, OverflowError):
        raise FinalizerInputError("outside ordered-finite-json-v2 domain") from None
    fields = {"value", "request_id", "candidate", "attempts", "packvm_response"}
    if operation == "stream":
        fields.add("attempt_number")
    if (type(value) is not dict or set(value) != fields
            or type(value["request_id"]) is not str
            or type(value["attempts"]) is not list
            or any(type(item) is not dict for item in value["attempts"])
            or value["packvm_response"] is not False):
        raise FinalizerInputError("invalid ordinary Gateway finalization request")
    if operation == "stream" and (
        type(value["attempt_number"]) is not int or value["attempt_number"] < 1
    ):
        raise FinalizerInputError("invalid recorded provider attempt")
    return value, _candidate(value["candidate"])


def invoke(operation: str, payload: str) -> str | DomainRejection:
    """Finalize once; preserve domain rejection without promoting imported errors."""
    value, candidate = _input(operation, payload)
    client = ImportedDependencies()
    try:
        arguments = (client, value["value"], value["request_id"], candidate)
        if operation == "generate":
            result = _finalize_generate_result(
                *arguments, value["attempts"], streaming=False, packvm_response=False,
            )
        else:
            result = _finalize_stream_result(
                *arguments, value["attempt_number"], value["attempts"],
                packvm_response=False,
            )
    except TerminalDependencyFailure:
        raise
    except GlobalContractInvocationError as error:
        # This is original finalizer-domain evidence, not an imported Core error.
        rejection = DomainRejection(error.code, str(error))
        if type(rejection.code) is not str or type(rejection.message) is not str:
            raise FinalizerProjectionFailure(
                "invalid original domain evidence",
            ) from None
        try:
            dumps({"code": rejection.code, "message": rejection.message})
        except (TypeError, ValueError, OverflowError):
            raise FinalizerProjectionFailure("domain evidence outside DATA") from None
        return rejection
    except (TypeError, ValueError, OverflowError, IndexError, AttributeError):
        raise FinalizerProjectionFailure("original finalization failed") from None
    try:
        return dumps(result)
    except (TypeError, ValueError, OverflowError):
        raise FinalizerProjectionFailure("finalized result outside DATA") from None
