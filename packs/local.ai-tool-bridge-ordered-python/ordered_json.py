"""Ordered finite JSON v1, independent of the host's CoreStrictV1 codec.

The wire value is a JSON string. Object order, string contents, float/int identity,
and binary64 values (including negative zero) survive a round trip. The domain
contains only exact native None/bool/int/float/str/list/dict values. Depth starts
at zero for the root; object keys count as nodes at their object's child depth.
"""

import json
import math
import re
from collections.abc import Iterator
from json.encoder import encode_basestring
from typing import Any

MAX_BYTES = 4 * 1024 * 1024
MAX_DEPTH = 64
MAX_NODES = 100_000
MAX_SAFE_INTEGER = (1 << 53) - 1
STRING_CHUNK_SIZE = 4096

# Each regex operates on at most STRING_CHUNK_SIZE characters. In particular,
# long strings and escape runs are consumed in native code, not character loops.
_STRING_RUN = re.compile(r'(?:[^"\\]|\\[\s\S])*')
_TOKEN = re.compile(r'["{}\[\]]|[^ \t\r\n,:{}\[\]"]+')
_ATOM = re.compile(r'[^ \t\r\n,:{}\[\]"]+')


class OrderedJSONError(ValueError):
    """A value or document is outside the ordered finite JSON v1 domain."""


class _Budget:
    def __init__(self) -> None:
        self.nodes = 0
        self.active: set[int] = set()

    def visit(self, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise OrderedJSONError("JSON depth exceeds 64")
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise OrderedJSONError("JSON node count exceeds 100000")


def _utf8_size(text: str) -> int:
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise OrderedJSONError("JSON strings must not contain surrogates") from exc


def _string_chunks(value: str) -> Iterator[str]:
    yield '"'
    for offset in range(0, len(value), STRING_CHUNK_SIZE):
        chunk = value[offset : offset + STRING_CHUNK_SIZE]
        _utf8_size(chunk)
        # Encoding separately is equivalent for Unicode scalar strings; no JSON
        # escape or surrogate-pair state can cross these input chunk boundaries.
        yield encode_basestring(chunk)[1:-1]
    yield '"'


def _iterencode(value: Any, budget: _Budget, depth: int) -> Iterator[str]:
    budget.visit(depth)
    kind = type(value)
    if value is None:
        yield "null"
    elif kind is bool:
        yield "true" if value else "false"
    elif kind is int:
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise OrderedJSONError("JSON integer exceeds the safe integer range")
        yield str(value)
    elif kind is float:
        if not math.isfinite(value):
            raise OrderedJSONError("JSON floats must be finite")
        yield repr(value)
    elif kind is str:
        yield from _string_chunks(value)
    elif kind is list or kind is dict:
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
        raise OrderedJSONError("JSON values must use exact native JSON types")


def dumps(value: Any) -> str:
    """Encode a native JSON value, preserving order and numeric identity.

    Raise OrderedJSONError for unsupported types, cycles, invalid Unicode or
    numbers, or a document exceeding 4 MiB UTF-8, depth 64, or 100000 nodes.
    No custom serialization hooks are called.
    """
    parts: list[str] = []
    size = 0
    for part in _iterencode(value, _Budget(), 0):
        size += _utf8_size(part)
        if size > MAX_BYTES:
            raise OrderedJSONError("JSON document exceeds 4 MiB UTF-8")
        parts.append(part)
    return "".join(parts)


def _preflight(text: str) -> None:
    """Bound UTF-8 size, depth, and nodes before the native JSON parser runs."""
    if len(text) > MAX_BYTES:
        raise OrderedJSONError("JSON document exceeds 4 MiB UTF-8")
    size = 0
    depth = 0
    nodes = 0
    in_string = False
    escaped = False
    in_atom = False
    for offset in range(0, len(text), STRING_CHUNK_SIZE):
        chunk = text[offset : offset + STRING_CHUNK_SIZE]
        size += _utf8_size(chunk)
        if size > MAX_BYTES:
            raise OrderedJSONError("JSON document exceeds 4 MiB UTF-8")
        position = 0
        while position < len(chunk):
            if in_string:
                if escaped:
                    position += 1
                    escaped = False
                match = _STRING_RUN.match(chunk, position)
                position = match.end()
                if position == len(chunk):
                    break
                if chunk[position] == '"':
                    in_string = False
                else:
                    # The only other unmatched character is a final backslash.
                    escaped = True
                position += 1
                continue
            if in_atom:
                match = _ATOM.match(chunk, position)
                in_atom = False
                if match is not None:
                    position = match.end()
                    if position == len(chunk):
                        in_atom = True
                        break
            match = _TOKEN.search(chunk, position)
            if match is None:
                break
            position = match.end()
            token = chunk[match.start()]
            if token == "}" or token == "]":
                depth -= 1
                if depth < 0:
                    raise OrderedJSONError("Invalid JSON nesting")
                continue
            if depth > MAX_DEPTH:
                raise OrderedJSONError("JSON depth exceeds 64")
            nodes += 1
            if nodes > MAX_NODES:
                raise OrderedJSONError("JSON node count exceeds 100000")
            if token == '"':
                in_string = True
            elif token == "{" or token == "[":
                depth += 1
            else:
                in_atom = position == len(chunk)


def _parse_integer(token: str) -> int:
    # Avoid converting arbitrarily long attacker-controlled integer tokens.
    if len(token) > 17:
        raise OrderedJSONError("JSON integer exceeds the safe integer range")
    value = int(token)
    if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
        raise OrderedJSONError("JSON integer exceeds the safe integer range")
    return value


def _parse_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise OrderedJSONError("JSON floats must be finite")
    return value


def _reject_constant(token: str) -> None:
    raise OrderedJSONError("JSON floats must be finite")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OrderedJSONError("JSON objects must not contain duplicate keys")
        result[key] = value
    return result


def loads(text: str) -> Any:
    """Decode one JSON string with the same domain and budgets as dumps.

    Duplicate keys, nonfinite numbers, lone surrogates, and non-string input
    are rejected. Strings that themselves contain JSON remain opaque strings.
    The 4 MiB limit applies to the supplied UTF-8 document, including whitespace.
    """
    if type(text) is not str:
        raise OrderedJSONError("JSON input must be an exact string")
    _preflight(text)
    try:
        value = json.loads(
            text,
            parse_int=_parse_integer,
            parse_float=_parse_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_pairs,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise OrderedJSONError("Invalid JSON document") from exc
    # Validate decoded Unicode, including escaped lone surrogates, and the
    # complete native tree. Do not constrain the size of a re-encoded spelling:
    # the decoder's byte budget applies to its input, not a normalized variant.
    for _ in _iterencode(value, _Budget(), 0):
        pass
    return value
