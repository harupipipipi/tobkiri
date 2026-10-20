"""Bounded caller adapter with one real generated bridge import per invocation.

No authority substitute, local normalization, Host, HTTP, retry, or fallback is
provided. Unit tests may install explicitly labelled generated-binding fixtures;
those fixtures are not part of this Pack and do not prove Wasm/Core execution.
"""

from typing import Any

from componentize_py_types import Err
from wit_world.imports import bridge

from ordered_json_v2 import dumps, loads
from original_gateway_tool_caller import (
    GlobalContractInvocationError,
    TOOL_BRIDGE_CONTRACT,
    TOOL_BRIDGE_GENERATE_OPERATION,
    TOOL_BRIDGE_STREAM_OPERATION,
    _attach_stream_tool_intents,
    _tool_intents,
)

PROTOCOL = "ordered-finite-json-v2"
OPERATIONS = ("generate-intents", "stream-intents", "stream-event-intents")
BRIDGE_OPERATIONS = (TOOL_BRIDGE_GENERATE_OPERATION, TOOL_BRIDGE_STREAM_OPERATION)


class CallerInputError(ValueError):
    """The bounded helper request is not valid; no provider call is attempted."""


class TerminalBridgeFailure(RuntimeError):
    """Failure never caught as the original retryable Gateway exception."""

    retryable = False


class ImportedBridgeFailure(TerminalBridgeFailure):
    """Keep every original typed imported error for unchanged app rethrow."""

    def __init__(self, core_error: Any) -> None:
        super().__init__("imported bridge failed")
        self.core_error = core_error


class BridgeTransportFailure(TerminalBridgeFailure):
    """Request encoding or import transport failed with no normalization replay."""


class BridgeResponseFailure(TerminalBridgeFailure):
    """A successful import returned unusable data; never replay normalization."""


class ImportedBridgeClient:
    """Only the original tool contract and two selected operation IDs are valid."""

    def invoke(self, contract: str, operation: str, payload: object) -> object:
        """Call the actual generated typed import once and preserve ordered DATA."""
        if (type(contract) is not str or contract != TOOL_BRIDGE_CONTRACT
                or type(operation) is not str or operation not in BRIDGE_OPERATIONS):
            raise CallerInputError("unsupported Gateway dependency")
        try:
            encoded = dumps(payload)
        except Exception:
            raise BridgeTransportFailure("bridge request cannot be encoded") from None
        try:
            result = bridge.normalize(operation, encoded)
        except Err as error:
            # In the real old Host path provider exceptions become terminal
            # ProviderExecutionError. INVALID must NOT become the original
            # GlobalContractInvocationError, which _invoke could retry.
            raise ImportedBridgeFailure(error.value) from None
        except Exception:
            raise BridgeTransportFailure("bridge import transport failed") from None
        try:
            return loads(result)
        except Exception:
            raise BridgeResponseFailure("bridge response cannot be decoded") from None


def _input(operation: str, payload: str) -> dict[str, Any]:
    if type(operation) is not str or operation not in OPERATIONS:
        raise CallerInputError("unknown Gateway helper operation")
    try:
        value = loads(payload)
    except RecursionError:
        raise
    except (ValueError, TypeError, OverflowError):
        raise CallerInputError("outside ordered-finite-json-v2 domain") from None
    collection = "events" if operation == "stream-event-intents" else "intents"
    if (type(value) is not dict or set(value) != {"request_id", collection}
            or type(value["request_id"]) is not str):
        raise CallerInputError("invalid Gateway helper request")
    # The original bridge, not this transport, owns non-list intent fallback.
    # Keep all finite DATA intact until that real dependency evaluates it.
    if collection == "events" and (
        type(value[collection]) is not list
        or any(type(event) is not dict for event in value[collection])
    ):
        raise CallerInputError("stream events must be objects")
    return value


def invoke(operation: str, payload: str) -> str:
    """Execute only the unchanged helper; never full generation or failover."""
    value = _input(operation, payload)
    client = ImportedBridgeClient()
    try:
        if operation == "stream-event-intents":
            result = value["events"]
            _attach_stream_tool_intents(client, result, value["request_id"])
        else:
            result = _tool_intents(
                client, value["intents"], value["request_id"],
                streaming=operation == "stream-intents",
            )
    except TerminalBridgeFailure:
        raise
    except (GlobalContractInvocationError, IndexError, TypeError, ValueError):
        # The verbatim helper can reject a malformed successful reply, including
        # a stream result with no usable first item. It cannot trigger a replay.
        raise BridgeResponseFailure(
            "bridge response violates helper contract",
        ) from None
    try:
        return dumps(result)
    except Exception:
        raise BridgeResponseFailure("helper result cannot be encoded") from None
