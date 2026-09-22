"""Fail-closed tombstone for the retired legacy tool projection."""

from __future__ import annotations

from typing import Any, Callable, Mapping


class LegacyToolProjectionRetired(RuntimeError):
    """Raised when a caller reaches the retired projection entrypoint."""


def _retired_operation(
    name: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    del name, payload
    raise LegacyToolProjectionRetired(
        "legacy default-tool projection is retired; use selected Tool Registry "
        "definitions and finite owner executors"
    )


def create_source_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Return a fail-closed compatibility entrypoint for old source bindings."""
    del client
    return _retired_operation


def create_local_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Return a fail-closed compatibility entrypoint for old executor bindings."""
    del client
    return _retired_operation
