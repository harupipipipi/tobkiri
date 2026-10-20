"""Source-only Python-on-Wasm caller with a required typed bridge@2 dependency."""

import platform
import unicodedata

from componentize_py_types import Err
import wit_world.exports
from wit_world.imports.types import Error, ErrorCode

from ordered_json_v2 import dumps
from gateway_tool_caller_adapter import (
    BridgeResponseFailure,
    BridgeTransportFailure,
    CallerInputError,
    ImportedBridgeFailure,
    invoke,
)


def _call(operation: str, payload: str) -> str:
    try:
        return invoke(operation, payload)
    except ImportedBridgeFailure as failure:
        # Preserve the original typed record, including denied/approval-required/
        # cancelled/invalid. Do not turn INVALID into a retryable Python failure.
        raise Err(failure.core_error) from None
    except (BridgeTransportFailure, BridgeResponseFailure):
        raise Err(Error(ErrorCode.INTERNAL, "Gateway tool bridge failed")) from None
    except RecursionError:
        raise Err(Error(
            ErrorCode.LIMIT_EXCEEDED, "Gateway tool caller resource limit exceeded",
        )) from None
    except CallerInputError:
        raise Err(Error(
            ErrorCode.INVALID, "invalid Gateway tool caller request",
        )) from None


class Lifecycle(wit_world.exports.Lifecycle):
    """Retain canonical resource-free lifecycle without authority initialization."""

    def init(self, config: str) -> None:
        """Acquire no resources, credentials, or configuration."""

    def shutdown(self) -> None:
        """Hold no external resources."""


class Caller(wit_world.exports.Caller):
    """Expose helpers, never impersonate the original four full Gateway callers."""

    def runtime_info(self) -> str:
        """Report the actual executing interpreter, not a producer receipt label."""
        return dumps({
            "python": platform.python_version(),
            "unicode": unicodedata.unidata_version,
        })

    def generate_intents(self, payload: str) -> str:
        """Run the original _tool_intents with streaming=False."""
        return _call("generate-intents", payload)

    def stream_intents(self, payload: str) -> str:
        """Run the original _tool_intents with streaming=True."""
        return _call("stream-intents", payload)

    def stream_event_intents(self, payload: str) -> str:
        """Run the original per-event stream helper in original order."""
        return _call("stream-event-intents", payload)
