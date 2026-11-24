"""Typed, resource-free export around the installed-defaults DATA adapter."""

import wit_world.exports
from componentize_py_types import Err
from wit_world.imports.types import Error, ErrorCode

from bundle_catalog_adapter import (
    BundleCatalogError,
    embedded_owner,
    read_catalog,
)

_READY = False


def _raise_core_error(error: BundleCatalogError) -> None:
    """Map adapter errors to closed Core codes without exposing exceptions."""
    if error.code == "invalid-request":
        code = ErrorCode.INVALID
    elif error.code == "limit-exceeded":
        code = ErrorCode.LIMIT_EXCEEDED
    else:
        code = ErrorCode.INTERNAL
    raise Err(Error(code, error.message)) from None


class Lifecycle(wit_world.exports.Lifecycle):
    """Retain only the immutable installed-defaults DATA owner."""

    def init(self, config: str) -> None:
        """Keep Core's small immutable config; defaults belong to the artifact."""
        global _READY
        _READY = False
        if config != "{}":
            raise Err(
                Error(
                    ErrorCode.INVALID, "bundle defaults do not accept runtime overrides"
                )
            ) from None
        _READY = True

    def shutdown(self) -> None:
        """Release the retained DATA owner."""
        global _READY
        _READY = False


class Catalog(wit_world.exports.Catalog):
    """Expose the original installed-defaults collection calculation."""

    def read(self) -> str:
        """Validate and serialize the five original source collections."""
        if not _READY:
            raise Err(
                Error(
                    ErrorCode.INVALID,
                    "bundle catalog is not initialized",
                )
            ) from None
        try:
            return read_catalog(embedded_owner())
        except BundleCatalogError as error:
            _raise_core_error(error)
