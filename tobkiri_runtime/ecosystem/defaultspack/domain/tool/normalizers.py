"""Small JSON-boundary normalizers with no higher-level tool dependencies."""

from typing import Any


def mapping_or_empty(value: Any) -> dict[str, Any]:
    """Normalize an untrusted JSON value to an object."""
    return value if isinstance(value, dict) else {}


def list_or_empty(value: Any) -> list[Any]:
    """Normalize an untrusted JSON value to a list."""
    return value if isinstance(value, list) else []
