"""Additive bounded-integer profile; the receipt-bound v1 codec is unchanged.

The wire value remains ordered finite JSON in an opaque string. Only the integer
domain changes: exact native integers with at most 4300 decimal digits, excluding
the sign. Existing Unicode, byte, depth, node, float and exact-type rules apply.
This module does not change the interpreter's integer-conversion safety setting.
"""

import json
from collections.abc import Iterator
from typing import Any

import ordered_json as v1

PROTOCOL = "ordered-finite-json-v2"
MAX_INTEGER_DIGITS = 4300
MAX_BYTES = v1.MAX_BYTES
MAX_DEPTH = v1.MAX_DEPTH
MAX_NODES = v1.MAX_NODES
_INTEGER_LIMIT = 10**MAX_INTEGER_DIGITS
OrderedJSONError = v1.OrderedJSONError


def _integer_token(value: int) -> str:
    # Compare before conversion, without copying a potentially huge negative int.
    if not -_INTEGER_LIMIT < value < _INTEGER_LIMIT:
        raise OrderedJSONError("JSON integer exceeds 4300 decimal digits")
    return str(value)


def _parse_integer(token: str) -> int:
    # json.loads validates JSON number grammar before invoking this hook.
    if len(token) - (1 if token.startswith("-") else 0) > MAX_INTEGER_DIGITS:
        raise OrderedJSONError("JSON integer exceeds 4300 decimal digits")
    return int(token)


def _iterencode(value: Any, budget: v1._Budget, depth: int) -> Iterator[str]:
    kind = type(value)
    if kind is int:
        budget.visit(depth)
        yield _integer_token(value)
    elif kind is list or kind is dict:
        budget.visit(depth)
        identity = id(value)
        if identity in budget.active:
            raise OrderedJSONError("JSON values must not contain cycles")
        minimum_children = len(value) * (2 if kind is dict else 1)
        if minimum_children > MAX_NODES - budget.nodes:
            raise OrderedJSONError("JSON node count exceeds 100000")
        budget.active.add(identity)
        try:
            yield "{" if kind is dict else "["
            first = True
            for item in value:
                if not first:
                    yield ","
                first = False
                if kind is dict:
                    if type(item) is not str:
                        raise OrderedJSONError("JSON object keys must be exact strings")
                    yield from _iterencode(item, budget, depth + 1)
                    yield ":"
                    yield from _iterencode(value[item], budget, depth + 1)
                else:
                    yield from _iterencode(item, budget, depth + 1)
            yield "}" if kind is dict else "]"
        finally:
            budget.active.remove(identity)
    else:
        # Delegate unchanged scalar handling, including bool before int identity,
        # finite binary64, negative zero, Unicode and unsupported native types.
        yield from v1._iterencode(value, budget, depth)


def dumps(value: Any) -> str:
    """Encode v2 without changing v1 or relaxing any existing resource budget."""
    parts: list[str] = []
    size = 0
    for part in _iterencode(value, v1._Budget(), 0):
        size += v1._utf8_size(part)
        if size > MAX_BYTES:
            raise OrderedJSONError("JSON document exceeds 4 MiB UTF-8")
        parts.append(part)
    return "".join(parts)


def loads(text: str) -> Any:
    """Decode bounded decimal integers while preserving v1's other semantics."""
    if type(text) is not str:
        raise OrderedJSONError("JSON input must be an exact string")
    v1._preflight(text)
    try:
        value = json.loads(
            text,
            parse_int=_parse_integer,
            parse_float=v1._parse_float,
            parse_constant=v1._reject_constant,
            object_pairs_hook=v1._object_pairs,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise OrderedJSONError("Invalid JSON document") from exc
    # As in v1, the byte budget measures input bytes, not re-encoded spelling.
    for _ in _iterencode(value, v1._Budget(), 0):
        pass
    return value
