"""Source-only Python-on-Wasm wrapper; no local authority or Host imports."""

import platform
import unicodedata

from componentize_py_types import Err
import wit_world.exports
from wit_world.imports.types import Error, ErrorCode

from ordered_json_v2 import dumps
from usage_cost_adapter import (
    ProviderArithmeticFailure,
    ProviderInputError,
    ProviderOutputFailure,
    ProviderValidationFailure,
    invoke,
)


class Lifecycle(wit_world.exports.Lifecycle):
    """Canonical resource-free lifecycle without credentials or initialization."""

    def init(self, config: str) -> None:
        """Acquire no resources or configuration."""

    def shutdown(self) -> None:
        """Hold no external resources."""


class UsageCost(wit_world.exports.UsageCost):
    """Expose only the original pure usage cost operation and diagnostics."""

    def runtime_info(self) -> str:
        """Report actual executing interpreter and Unicode versions."""
        return dumps({
            "python": platform.python_version(),
            "unicode": unicodedata.unidata_version,
        })

    def calculate(self, operation: str, payload: str) -> str:
        """Keep typed failure categories terminal without private exception text."""
        try:
            return invoke(operation, payload)
        except ProviderInputError:
            raise Err(Error(
                ErrorCode.INVALID, "invalid usage cost request",
            )) from None
        except ProviderValidationFailure:
            raise Err(Error(
                ErrorCode.INTERNAL, "usage cost provider validation failed",
            )) from None
        except ProviderArithmeticFailure:
            raise Err(Error(
                ErrorCode.INTERNAL, "usage cost provider arithmetic failed",
            )) from None
        except ProviderOutputFailure:
            raise Err(Error(
                ErrorCode.INTERNAL, "usage cost provider output failed",
            )) from None
        except (RecursionError, MemoryError):
            raise Err(Error(
                ErrorCode.LIMIT_EXCEEDED, "usage cost resource limit exceeded",
            )) from None
