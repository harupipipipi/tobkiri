"""Unshipped bounded v2 profile; the receipt-bound v1 codec is unchanged.

The wire value is ordered finite JSON in an opaque string. Exact native integers
have at most 4300 decimal digits, excluding the sign. The existing byte and node
budgets bound iterative traversal without an independent nesting-depth cap.
The native JSON decoder can still raise its real runtime RecursionError. Neither
the recursion limit nor the integer-conversion safety setting is changed.
"""

import json
import math
from collections.abc import Iterator
from typing import Any

import ordered_json as v1

PROTOCOL = "ordered-finite-json-v2"
MAX_INTEGER_DIGITS = 4300
MAX_BYTES = v1.MAX_BYTES
MAX_NODES = v1.MAX_NODES
_INTEGER_LIMIT = 10**MAX_INTEGER_DIGITS
OrderedJSONError = v1.OrderedJSONError


class NonFiniteJSONError(OrderedJSONError):
    """An exact float reached encoding but is not finite."""


class _Budget:
    """Count all value/key occurrences and track only active containers."""

    def __init__(self) -> None:
        self.nodes = 0
        self.active: set[int] = set()

    def visit(self, depth: int) -> None:
        # Keep the scalar-helper calling convention without imposing a separate
        # depth policy. Every path edge consumes a node, including object keys.
        del depth
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise OrderedJSONError("JSON node count exceeds 100000")


class _Frame:
    """One active container, with children consumed rather than queued."""

    __slots__ = ("value", "items", "depth", "first")

    def __init__(self, value: list[Any] | dict[str, Any], depth: int) -> None:
        self.value = value
        self.items = iter(value)
        self.depth = depth
        self.first = True


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


def _iterencode(value: Any, budget: _Budget, depth: int) -> Iterator[str]:
    frames: list[_Frame] = []
    try:
        while True:
            kind = type(value)
            if kind is int:
                budget.visit(depth)
                yield _integer_token(value)
            elif kind is float:
                budget.visit(depth)
                if not math.isfinite(value):
                    raise NonFiniteJSONError("JSON floats must be finite")
                yield repr(value)
            elif kind is list or kind is dict:
                budget.visit(depth)
                identity = id(value)
                if identity in budget.active:
                    raise OrderedJSONError("JSON values must not contain cycles")
                minimum_children = len(value) * (2 if kind is dict else 1)
                if minimum_children > MAX_NODES - budget.nodes:
                    raise OrderedJSONError("JSON node count exceeds 100000")
                budget.active.add(identity)
                frames.append(_Frame(value, depth))
                yield "{" if kind is dict else "["
            else:
                # This call handles only scalars or rejected non-native types;
                # container traversal never recurses through the v1 encoder.
                yield from v1._iterencode(value, budget, depth)

            while frames:
                frame = frames[-1]
                try:
                    item = next(frame.items)
                except StopIteration:
                    frames.pop()
                    budget.active.remove(id(frame.value))
                    yield "}" if type(frame.value) is dict else "]"
                    continue
                if not frame.first:
                    yield ","
                frame.first = False
                depth = frame.depth + 1
                if type(frame.value) is dict:
                    if type(item) is not str:
                        raise OrderedJSONError("JSON object keys must be exact strings")
                    yield from v1._iterencode(item, budget, depth)
                    yield ":"
                    value = frame.value[item]
                else:
                    value = item
                break
            else:
                return
    finally:
        for frame in frames:
            budget.active.discard(id(frame.value))


def dumps(value: Any) -> str:
    """Encode v2 iteratively within the unchanged byte and node budgets."""
    parts: list[str] = []
    size = 0
    for part in _iterencode(value, _Budget(), 0):
        size += v1._utf8_size(part)
        if size > MAX_BYTES:
            raise OrderedJSONError("JSON document exceeds 4 MiB UTF-8")
        parts.append(part)
    return "".join(parts)


def _preflight(text: str) -> None:
    """Bound bytes and token nodes before native parsing, leaving strings opaque."""
    if len(text) > MAX_BYTES:
        raise OrderedJSONError("JSON document exceeds 4 MiB UTF-8")
    size = 0
    depth = 0
    nodes = 0
    in_string = False
    escaped = False
    in_atom = False
    for offset in range(0, len(text), v1.STRING_CHUNK_SIZE):
        chunk = text[offset : offset + v1.STRING_CHUNK_SIZE]
        size += v1._utf8_size(chunk)
        if size > MAX_BYTES:
            raise OrderedJSONError("JSON document exceeds 4 MiB UTF-8")
        position = 0
        while position < len(chunk):
            if in_string:
                if escaped:
                    position += 1
                    escaped = False
                match = v1._STRING_RUN.match(chunk, position)
                position = match.end()
                if position == len(chunk):
                    break
                if chunk[position] == '"':
                    in_string = False
                else:
                    escaped = True
                position += 1
                continue
            if in_atom:
                match = v1._ATOM.match(chunk, position)
                in_atom = False
                if match is not None:
                    position = match.end()
                    if position == len(chunk):
                        in_atom = True
                        break
            match = v1._TOKEN.search(chunk, position)
            if match is None:
                break
            position = match.end()
            token = chunk[match.start()]
            if token == "}" or token == "]":
                depth -= 1
                if depth < 0:
                    raise OrderedJSONError("Invalid JSON nesting")
                continue
            nodes += 1
            if nodes > MAX_NODES:
                raise OrderedJSONError("JSON node count exceeds 100000")
            if token == '"':
                in_string = True
            elif token == "{" or token == "[":
                depth += 1
            else:
                in_atom = position == len(chunk)


def loads(text: str) -> Any:
    """Decode bounded JSON, retaining native decoder resource failures.

    Byte and node limits are resource caps, not a promise that every in-budget
    document fits the current interpreter or guest runtime. In particular,
    RecursionError is not converted into malformed-JSON ValueError.
    """
    if type(text) is not str:
        raise OrderedJSONError("JSON input must be an exact string")
    _preflight(text)
    try:
        value = json.loads(
            text,
            parse_int=_parse_integer,
            parse_float=v1._parse_float,
            parse_constant=v1._reject_constant,
            object_pairs_hook=v1._object_pairs,
        )
    except json.JSONDecodeError as exc:
        raise OrderedJSONError("Invalid JSON document") from exc
    # As in v1, the byte budget measures input bytes, not re-encoded spelling.
    for _ in _iterencode(value, _Budget(), 0):
        pass
    return value
