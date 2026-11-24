"""Typed Python component adapter; no host factory or authority emulation."""
import platform
import unicodedata
import wit_world.exports
from ordered_json import dumps
from componentize_py_types import Err
from wit_world.imports.types import Error, ErrorCode
from caller_adapter import invoke
from original_bridge import GlobalContractInvocationError


class Lifecycle(wit_world.exports.Lifecycle):
    """Pure Pack lifecycle."""

    def init(self, config: str) -> None:
        """No external resources or configuration are consumed."""

    def shutdown(self) -> None:
        """No external resources are held."""


class Bridge(wit_world.exports.Bridge):
    """Expose the captured original normalizer through its ordered domain."""

    def runtime_info(self) -> str:
        """Report interpreter facts from this actual Wasm guest."""
        return dumps({"python": platform.python_version(),
                      "unicode": unicodedata.unidata_version})

    def normalize(self, operation: str, payload: str) -> str:
        """Map transport exclusions to typed invalid, not semantic parity."""
        try:
            return invoke(operation, payload)
        except (GlobalContractInvocationError, ValueError, TypeError,
                OverflowError, RecursionError):
            raise Err(Error(ErrorCode.INVALID,
                            "outside ordered-finite-json-v1 domain")) from None
