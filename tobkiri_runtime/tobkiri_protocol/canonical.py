"""Strict I-JSON parsing and deterministic content identity.

The runtime protocol uses a deliberately conservative subset of JSON.  JSON
objects with duplicate keys, non-finite numbers, floating point values, deep
nesting, oversized input, or invalid UTF-8 are rejected before schema
validation.  Rejecting rather than guessing keeps a later authority decision
from depending on parser differences between languages.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .errors import CanonicalizationError

MAX_CANONICAL_JSON_BYTES = 4 * 1024 * 1024
MAX_CANONICAL_JSON_DEPTH = 64
MAX_SAFE_INTEGER = (2**53) - 1


def strict_loads(
    value: str | bytes,
    *,
    max_bytes: int = MAX_CANONICAL_JSON_BYTES,
    max_depth: int = MAX_CANONICAL_JSON_DEPTH,
) -> Any:
    """Parse JSON while rejecting ambiguous or resource-exhausting input.

    Args:
        value: UTF-8 JSON text or bytes.
        max_bytes: Maximum encoded input size.
        max_depth: Maximum object/array nesting depth.

    Returns:
        A JSON-compatible Python value.

    Raises:
        CanonicalizationError: If the input violates the strict profile.
    """
    if not isinstance(value, (str, bytes)):
        raise CanonicalizationError("input must be UTF-8 JSON text or bytes")
    if isinstance(value, bytes):
        if len(value) > max_bytes:
            raise CanonicalizationError("json input exceeds size limit")
        try:
            text = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise CanonicalizationError("json input is not valid UTF-8") from exc
    else:
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise CanonicalizationError("json input contains invalid Unicode") from exc
        if len(encoded) > max_bytes:
            raise CanonicalizationError("json input exceeds size limit")
        text = value

    def reject_constant(token: str) -> None:
        raise CanonicalizationError(f"non-finite JSON number: {token}")

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise CanonicalizationError(f"duplicate JSON object key: {key!r}")
            result[key] = item
        return result

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=reject_duplicate,
            parse_constant=reject_constant,
        )
    except CanonicalizationError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CanonicalizationError(f"invalid JSON: {exc}") from exc
    _validate_value(parsed, depth=0, max_depth=max_depth)
    return parsed


def canonical_json(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes for a strict JSON value."""
    _validate_value(value, depth=0, max_depth=MAX_CANONICAL_JSON_DEPTH)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CanonicalizationError(f"value is not canonical JSON: {exc}") from exc
    if len(encoded) > MAX_CANONICAL_JSON_BYTES:
        raise CanonicalizationError("canonical JSON exceeds size limit")
    return encoded


def canonical_bytes(value: Mapping[str, Any]) -> bytes:
    """Canonicalize one object and return its serialized bytes."""
    if not isinstance(value, Mapping):
        raise CanonicalizationError("canonical document must be an object")
    return canonical_json(dict(value))


def canonical_digest(value: Any) -> str:
    """Return a lowercase SHA-256 digest of canonical JSON."""
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def _validate_value(value: Any, *, depth: int, max_depth: int) -> None:
    if depth > max_depth:
        raise CanonicalizationError("json nesting exceeds depth limit")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise CanonicalizationError(
                "integer exceeds I-JSON safe range; use a canonical decimal string"
            )
        return
    if isinstance(value, float):
        raise CanonicalizationError(
            "floating point values are not permitted; use an exact string or integer"
        )
    if isinstance(value, list):
        for item in value:
            _validate_value(item, depth=depth + 1, max_depth=max_depth)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError("JSON object keys must be strings")
            _validate_value(item, depth=depth + 1, max_depth=max_depth)
        return
    raise CanonicalizationError(f"unsupported JSON value type: {type(value).__name__}")
