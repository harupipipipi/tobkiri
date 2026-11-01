"""Lossless JSON cells for the explicitly versioned storage 1.1 contract."""

from __future__ import annotations

import json
from typing import Any

from .floats import to_canon_float

I64_MIN = -(2**63)
I64_MAX = 2**63 - 1


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("SQL text value must be Unicode text")
    # Surrogate code points cannot be transmitted as valid Unicode UTF-8.
    value.encode("utf-8", errors="strict")
    return value


def integer(value: Any) -> int:
    """Decode one canonical signed 64-bit decimal string."""
    if not isinstance(value, str):
        raise ValueError("SQL integer must be a canonical decimal string")
    try:
        number = int(value)
    except ValueError:
        raise ValueError("invalid SQL integer") from None
    if not I64_MIN <= number <= I64_MAX or str(number) != value:
        raise ValueError("noncanonical or out-of-range SQL integer")
    return number


def encode_cell(value: Any) -> str:
    """Encode only the named DB-API scalar types; never infer a cell from a dict."""
    if value is None:
        cell = {"type": "null", "value": None}
    elif type(value) is int:
        if not I64_MIN <= value <= I64_MAX:
            raise ValueError("SQL integer is outside signed 64-bit range")
        cell = {"type": "integer", "value": str(value)}
    elif type(value) is float:
        cell = {"type": "real", "value": to_canon_float(value, "f64")}
    elif type(value) is str:
        cell = {"type": "text", "value": _text(value)}
    elif type(value) is bytes:
        cell = {"type": "blob", "value": list(value)}
    else:
        raise TypeError("SQL parameters support None, int, float, str and bytes")
    return json.dumps(cell, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":"))


def _object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate key in SQL cell")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise ValueError("non-finite JSON value in SQL cell")


def decode_cell(encoded: str) -> None | int | float | str | bytes:
    """Validate exactly one tagged cell and preserve its declared scalar type."""
    cell = json.loads(encoded, object_pairs_hook=_object, parse_constant=_constant)
    if not isinstance(cell, dict) or set(cell) != {"type", "value"}:
        raise ValueError("SQL cell must contain exactly type and value")
    kind, value = cell["type"], cell["value"]
    if kind == "null" and value is None:
        return None
    if kind == "integer":
        return integer(value)
    if kind == "real":
        if not isinstance(value, str):
            raise ValueError("SQL real must be a canonical scientific string")
        try:
            number = float(value)
        except ValueError:
            raise ValueError("invalid SQL real") from None
        if to_canon_float(number, "f64") != value:
            raise ValueError("noncanonical SQL real")
        return number
    if kind == "text":
        return _text(value)
    if kind == "blob":
        if (not isinstance(value, list)
                or any(type(byte) is not int or not 0 <= byte <= 255
                       for byte in value)):
            raise ValueError("SQL blob must be a list of bytes")
        return bytes(value)
    raise ValueError("invalid SQL cell tag or null value")
