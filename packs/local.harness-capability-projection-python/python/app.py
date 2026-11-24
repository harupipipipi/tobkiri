"""Typed, resource-free Wasm export for capability target DATA projection."""

import wit_world.exports
from componentize_py_types import Err
from wit_world.imports.types import Error, ErrorCode

from projection_adapter import ProjectionError, project


class Lifecycle(wit_world.exports.Lifecycle):
    """Expose the canonical resource-free Pack lifecycle."""

    def init(self, config: str) -> None:
        """Acquire no resources, configuration, or authority."""

    def shutdown(self) -> None:
        """Hold no external resources."""


class Projection(wit_world.exports.Projection):
    """Expose the original application capability projection as inert DATA."""

    def project(self, payload: str) -> str:
        """Compute target DATA; never approve or authorize a target."""
        try:
            return project(payload)
        except ProjectionError as error:
            if error.code == "invalid-request":
                raise Err(Error(ErrorCode.INVALID, error.message)) from None
            raise Err(Error(ErrorCode.INTERNAL, error.message)) from None
