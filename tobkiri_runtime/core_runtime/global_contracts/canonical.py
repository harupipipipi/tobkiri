"""Canonical serialization and content-identity helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes for a JSON-compatible value.

    Non-finite numbers and values outside the JSON data model are rejected so
    callers never receive a platform-dependent identity.
    """
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not canonical JSON: {exc}") from exc
    return rendered.encode("utf-8")


def content_identity(value: Any) -> str:
    """Return the versioned SHA-256 identity of a canonical JSON value."""
    digest = hashlib.sha256(canonical_json(value)).hexdigest()
    return f"sha256:{digest}"
