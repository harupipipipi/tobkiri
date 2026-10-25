"""Typed Python-on-Wasm exports for pure original Setup projections only."""

from componentize_py_types import Err
import wit_world.exports
from wit_world.imports.types import Error, ErrorCode

from setup_adapter import (
    SetupInputError, SetupOutputError, SetupProjectionError, invoke,
)


def _project(operation: str, payload: str) -> str:
    try:
        return invoke(operation, payload)
    except SetupInputError:
        raise Err(Error(ErrorCode.INVALID, "invalid Setup projection input")) from None
    except (SetupProjectionError, SetupOutputError):
        raise Err(Error(ErrorCode.INTERNAL, "Setup projection failed")) from None
    except (RecursionError, MemoryError):
        raise Err(Error(ErrorCode.LIMIT_EXCEEDED, "Setup resource limit exceeded")) from None


class Lifecycle(wit_world.exports.Lifecycle):
    """Resource-free lifecycle with no credentials or activation side effects."""

    def init(self, config: str) -> None:
        """Acquire no authority or resources."""

    def shutdown(self) -> None:
        """Hold no external resources."""


class Setup(wit_world.exports.Setup):
    """Project original application DATA; never commit a Profile or approval."""

    def decision(self, payload: str) -> str:
        """Return the original accept/reject DATA for an exact request."""
        return _project("decision", payload)

    def success(self, payload: str) -> str:
        """Project recorded success facts, without certifying their authority."""
        return _project("success", payload)

    def failure(self, payload: str) -> str:
        """Return the original no-write activation failure DATA."""
        return _project("failure", payload)

    def retired(self, payload: str) -> str:
        """Return the original retired-route DATA."""
        return _project("retired", payload)
