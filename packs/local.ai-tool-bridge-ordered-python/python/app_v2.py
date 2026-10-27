"""Versioned v2 component source; no host factory or authority emulation."""

import platform
import unicodedata

from componentize_py_types import Err
import wit_world.exports
from wit_world.imports.types import Error, ErrorCode

from caller_adapter_v2 import NonFiniteProviderOutputError, invoke
from ordered_json_v2 import dumps
from original_bridge import GlobalContractInvocationError


class Lifecycle(wit_world.exports.Lifecycle):
    """Retain the canonical, resource-free Pack lifecycle."""

    def init(self, config: str) -> None:
        """Consume no external resources or configuration."""

    def shutdown(self) -> None:
        """Hold no external resources."""


class Bridge(wit_world.exports.Bridge):
    """Expose the unchanged original normalizer through the v2 transport."""

    def runtime_info(self) -> str:
        """Report facts from the interpreter executing this component source."""
        return dumps(
            {
                "python": platform.python_version(),
                "unicode": unicodedata.unidata_version,
            }
        )

    def normalize(self, operation: str, payload: str) -> str:
        """Keep provider failure, runtime exhaustion and invalid input distinct."""
        try:
            return invoke(operation, payload)
        except NonFiniteProviderOutputError:
            raise Err(
                Error(
                    ErrorCode.INTERNAL,
                    "provider output contains a non-finite number",
                )
            ) from None
        except RecursionError:
            raise Err(
                Error(
                    ErrorCode.LIMIT_EXCEEDED,
                    "ordered tool bridge resource limit exceeded",
                )
            ) from None
        except (GlobalContractInvocationError, ValueError, TypeError, OverflowError):
            raise Err(
                Error(ErrorCode.INVALID, "outside ordered-finite-json-v2 domain")
            ) from None
