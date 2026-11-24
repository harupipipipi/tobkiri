"""Source-only finalizer with typed domain outcome and terminal Core errors."""

import platform
import unicodedata

from componentize_py_types import Err
import wit_world.exports
from wit_world.exports.finalizer import (
    DomainError,
    FinalizationOutcome,
    FinalizationOutcome_DomainRejected,
    FinalizationOutcome_Finalized,
)
from wit_world.imports.types import Error, ErrorCode

from ordered_json_v2 import dumps
from gateway_finalizer_adapter import (
    DomainRejection,
    FinalizerInputError,
    ImportedDependencyFailure,
    TerminalDependencyFailure,
    invoke,
)


def _call(operation: str, payload: str) -> FinalizationOutcome:
    try:
        result = invoke(operation, payload)
        if isinstance(result, DomainRejection):
            return FinalizationOutcome_DomainRejected(
                DomainError(result.code, result.message),
            )
        return FinalizationOutcome_Finalized(result)
    except ImportedDependencyFailure as failure:
        raise Err(failure.core_error) from None
    except TerminalDependencyFailure:
        raise Err(Error(ErrorCode.INTERNAL, "Gateway finalization failed")) from None
    except (RecursionError, MemoryError):
        raise Err(Error(
            ErrorCode.LIMIT_EXCEEDED, "Gateway finalization resource limit exceeded",
        )) from None
    except FinalizerInputError:
        raise Err(Error(
            ErrorCode.INVALID, "invalid Gateway finalization request",
        )) from None


class Lifecycle(wit_world.exports.Lifecycle):
    """Resource-free lifecycle, without credentials or authority initialization."""

    def init(self, config: str) -> None:
        """Acquire no resources or authority from configuration."""

    def shutdown(self) -> None:
        """Hold no external resources."""


class Finalizer(wit_world.exports.Finalizer):
    """Expose finalization, never a full Gateway or one of its caller identities."""

    def runtime_info(self) -> str:
        """Report the executing interpreter, without invoking dependencies."""
        return dumps({"python": platform.python_version(),
                      "unicode": unicodedata.unidata_version})

    def generate(self, payload: str) -> FinalizationOutcome:
        """Finalize recorded non-streaming provider DATA exactly once."""
        return _call("generate", payload)

    def stream(self, payload: str) -> FinalizationOutcome:
        """Finalize recorded streaming provider DATA exactly once."""
        return _call("stream", payload)
