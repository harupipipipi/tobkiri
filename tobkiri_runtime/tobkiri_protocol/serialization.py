"""Schema-aware serialization helpers with no runtime imports."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .canonical import canonical_bytes, strict_loads
from .validation import validate_document


def load_json_document(path: Path, schema_name: str) -> dict[str, Any]:
    """Load one strict JSON document and validate it against a named schema."""
    return validate_document(path.read_bytes(), schema_name)


def parse_json_document(value: str | bytes, schema_name: str) -> dict[str, Any]:
    """Parse one JSON document under the protocol's strict I-JSON profile."""
    return validate_document(value, schema_name)


def canonical_document(value: Mapping[str, Any], schema_name: str) -> bytes:
    """Validate a document and return its canonical serialized bytes."""
    normalized = validate_document(value, schema_name)
    return canonical_bytes(normalized)


def load_unvalidated_json(path: Path) -> Any:
    """Read JSON for scanner diagnostics without treating it as valid input."""
    return strict_loads(path.read_bytes())
