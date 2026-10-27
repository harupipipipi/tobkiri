"""Thin typed bindings for the non-production native input stack probe."""

from componentize_py_types import Err
import wit_world.exports
from wit_world.exports.probe import Error, ErrorCode
from wit_world.imports import captured

from logic import AlreadyInitializedError, InvalidInputError, Logic

# The pinned componentizer constructs an adapter object for each exported
# method. Keep application state once in this guest interpreter, not in those
# distinct adapter objects. Explicit domain initialization still happens in init.
_logic = Logic()


def _read_range(ordinal: int, offset: int, length: int) -> bytes:
    """Adapt WIT ``list<u8>`` output to the original reader's byte contract."""
    return bytes(captured.read_range(ordinal, offset, length))


class Probe(wit_world.exports.Probe):
    """Expose one init and one original-verifier invocation."""

    def __init__(self) -> None:
        self._logic = _logic

    def init(self, payload: str) -> None:
        """Bind one strict DATA frame without performing captured reads."""
        try:
            self._logic.init(payload)
        except InvalidInputError:
            raise Err(Error(
                ErrorCode.INVALID_INPUT,
                "invalid native input probe frame",
                "",
            )) from None
        except AlreadyInitializedError:
            raise Err(Error(
                ErrorCode.ALREADY_INITIALIZED,
                "native input probe is already initialized",
                "",
            )) from None

    def run(self) -> str:
        """Await the existing imported read-range path through sync Python."""
        return self._logic.run(_read_range)
