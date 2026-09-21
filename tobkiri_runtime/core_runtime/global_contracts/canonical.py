"""Canonical serialization and content identity helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes for a JSON-compatible value."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def content_identity(value: Any) -> str:
    """Return the versioned SHA-256 identity of a canonical JSON value."""
    digest = hashlib.sha256(canonical_json(value)).hexdigest()
    return f"sha256:{digest}"

