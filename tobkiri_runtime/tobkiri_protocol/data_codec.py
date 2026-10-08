"""Bounded runtime data codec; never substitute for authority canonicalization.

Legacy values retain exactly their canonical JSON bytes and SHA-256 digests.
Float-bearing values use a flat prefix-token tree with explicit v1 encoding.
The flat form keeps wire nesting bounded independently of logical nesting.

Use decode_record for durable data. The low-level decode_payload API requires
an explicit trusted caller assertion that its encoding marker came from an
already-authenticated enclosing record. Never copy that assertion from JSON.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import struct
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

VERSION = "tobkiri.flow-data.ieee754.v1"
RECORD_MARKER = "_tobkiri_flow_data_encoding"
DOMAIN = b"\x00tobkiri.flow-data.ieee754.v1\x00"
RECORD_DOMAIN = b"\x00tobkiri.flow-value-record.v1\x00"
MAX_SAFE_INTEGER = (2**53) - 1
_HEX_FLOAT = re.compile(r"[0-9a-f]{16}\Z")
Path = tuple[str | int, ...]


class CodecError(ValueError):
    """A value or encoded record violates the data codec's bounded profile."""


@dataclass(frozen=True)
class Limits:
    """Independent byte, logical-depth and node bounds for one value/record."""

    max_bytes: int = 4 * 1024 * 1024
    max_depth: int = 64
    max_nodes: int = 100_000

    def __post_init__(self) -> None:
        for value in (self.max_bytes, self.max_depth, self.max_nodes):
            if type(value) is not int or value < 1:
                raise CodecError("limits must be positive integers")
        if self.max_depth > 128:
            raise CodecError("depth bound cannot exceed 128")


DEFAULT_LIMITS = Limits()


@dataclass(frozen=True)
class EncodedPayload:
    """Canonical-safe bytes plus a separate runtime-owned encoding marker."""

    encoding: str | None
    payload: bytes


class _Budget:
    def __init__(
        self, limits: Limits, *, wire_paths: tuple[Path, ...] | None = None,
    ) -> None:
        self.limits = limits
        self.nodes = 0
        self.string_bytes = 0
        self.max_depth = limits.max_depth
        self.max_nodes = limits.max_nodes
        if wire_paths is not None:
            # Flat value tokens can add one level and at most ~2x nodes.
            # The authenticated marker also duplicates selected path strings.
            # Keep that overhead bounded without charging it as logical data.
            self.max_depth = max(4, limits.max_depth + 1)
            self.max_nodes = (
                4 * limits.max_nodes
                + sum(len(path) + 1 for path in wire_paths) + 16
            )

    def node(self, depth: int) -> None:
        self.nodes += 1
        if depth > self.max_depth:
            raise CodecError("logical nesting exceeds depth limit")
        if self.nodes > self.max_nodes:
            raise CodecError("value exceeds node limit")

    def string(self, value: str) -> None:
        if len(value) > self.limits.max_bytes:
            raise CodecError("string exceeds byte limit")
        try:
            self.string_bytes += len(value.encode("utf-8", errors="strict"))
        except UnicodeError as error:
            raise CodecError("string contains invalid Unicode") from error
        if self.string_bytes > self.limits.max_bytes:
            raise CodecError("strings exceed byte limit")


def _normalize(
    value: Any, limits: Limits, *, wire_paths: tuple[Path, ...] | None = None,
) -> tuple[Any, bool]:
    budget = _Budget(limits, wire_paths=wire_paths)
    active: set[int] = set()
    has_float = False

    def visit(item: Any, depth: int) -> Any:
        nonlocal has_float
        budget.node(depth)
        kind = type(item)
        if item is None or kind is bool:
            return item
        if kind is str:
            budget.string(item)
            return item
        if kind is int:
            if abs(item) > MAX_SAFE_INTEGER:
                raise CodecError("integer exceeds I-JSON safe range")
            return item
        if kind is float:
            if not math.isfinite(item):
                raise CodecError("non-finite float is not permitted")
            has_float = True
            return item
        if kind not in (dict, list):
            raise CodecError(f"unsupported value type: {kind.__name__}")
        if id(item) in active:
            raise CodecError("cyclic value is not permitted")
        active.add(id(item))
        try:
            if kind is list:
                return [visit(child, depth + 1) for child in item]
            result: dict[str, Any] = {}
            for key, child in item.items():
                if type(key) is not str:
                    raise CodecError("object keys must be strings")
                budget.node(depth)
                budget.string(key)
                result[key] = visit(child, depth + 1)
            return result
        finally:
            active.remove(id(item))

    return visit(value, 0), has_float


def _canonical(value: Any, limits: Limits) -> bytes:
    # Iteration avoids first allocating the entire oversized serialized value.
    encoder = json.JSONEncoder(
        ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True,
    )
    result = bytearray()
    try:
        for chunk in encoder.iterencode(value):
            raw = chunk.encode("utf-8", errors="strict")
            if len(result) + len(raw) > limits.max_bytes:
                raise CodecError("encoded value exceeds byte limit")
            result.extend(raw)
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        if isinstance(error, CodecError):
            raise
        raise CodecError("value cannot be serialized") from error
    return bytes(result)


def _tokens(value: Any) -> list[Any]:
    tokens: list[Any] = []

    def emit(item: Any) -> None:
        kind = type(item)
        if item is None:
            tokens.append("n")
        elif kind is bool:
            tokens.extend(("b", item))
        elif kind is int:
            tokens.extend(("i", item))
        elif kind is float:
            tokens.extend(("f", struct.pack(">d", item).hex()))
        elif kind is str:
            tokens.extend(("s", item))
        elif kind is list:
            tokens.extend(("a", len(item)))
            for child in item:
                emit(child)
        else:
            tokens.extend(("o", len(item)))
            for key in sorted(item):
                tokens.append(key)
                emit(item[key])

    emit(value)
    return tokens


def encode_payload(value: Any, *, limits: Limits = DEFAULT_LIMITS) -> EncodedPayload:
    """Validate and encode one data value without granting it any authority."""
    normalized, has_float = _normalize(value, limits)
    if not has_float:
        return EncodedPayload(None, _canonical(normalized, limits))
    return EncodedPayload(VERSION, _canonical(_tokens(normalized), limits))


def digest_payload(value: Any, *, limits: Limits = DEFAULT_LIMITS) -> str:
    """Preserve legacy digests; domain-separate every float-bearing value."""
    encoded = encode_payload(value, limits=limits)
    preimage = encoded.payload if encoded.encoding is None else DOMAIN + encoded.payload
    return "sha256:" + hashlib.sha256(preimage).hexdigest()


def _raw(value: bytes | str, limits: Limits) -> bytes:
    if type(value) is str:
        if len(value) > limits.max_bytes:
            raise CodecError("input exceeds byte limit")
        try:
            value = value.encode("utf-8", errors="strict")
        except UnicodeError as error:
            raise CodecError("input contains invalid Unicode") from error
    if type(value) is not bytes or len(value) > limits.max_bytes:
        raise CodecError("input must be bounded UTF-8 JSON")
    return value


def _parse(raw: bytes, limits: Limits, *, finite_floats: bool = False) -> Any:
    def reject_number(token: str) -> Any:
        raise CodecError("bare floating-point or non-finite number is forbidden")

    def integer(token: str) -> int:
        if len(token.lstrip("-")) > 16:
            raise CodecError("integer exceeds I-JSON safe range")
        value = int(token)
        if abs(value) > MAX_SAFE_INTEGER:
            raise CodecError("integer exceeds I-JSON safe range")
        return value

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CodecError("duplicate object key")
            result[key] = value
        return result

    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(
            text, parse_float=float if finite_floats else reject_number,
            parse_constant=reject_number,
            parse_int=integer, object_pairs_hook=unique,
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, CodecError):
            raise
        raise CodecError("invalid or excessively nested JSON") from error


def loads_data_json(
    payload: bytes | str, *, limits: Limits = DEFAULT_LIMITS,
) -> Any:
    """Parse untrusted ordinary JSON data without interpreting encoding tags.

    Reject duplicate keys, unsafe integers, non-finite values, invalid Unicode
    and resource limits. Callers admitting a mixed authority/data envelope must
    separately apply its trusted selective field policy before using it.
    """
    raw = _raw(payload, limits)
    result, _ = _normalize(_parse(raw, limits, finite_floats=True), limits)
    return result


def _untokens(tokens: Any, limits: Limits) -> Any:
    if type(tokens) is not list or len(tokens) > 3 * limits.max_nodes:
        raise CodecError("invalid token stream")
    budget = _Budget(limits)
    index = 0
    has_float = False

    def take() -> Any:
        nonlocal index
        if index >= len(tokens):
            raise CodecError("truncated token stream")
        value = tokens[index]
        index += 1
        return value

    def visit(depth: int) -> Any:
        nonlocal has_float
        budget.node(depth)
        tag = take()
        if type(tag) is not str:
            raise CodecError("invalid type tag")
        if tag == "n":
            return None
        value = take()
        if tag == "b" and type(value) is bool:
            return value
        if tag == "i" and type(value) is int and abs(value) <= MAX_SAFE_INTEGER:
            return value
        if tag == "s" and type(value) is str:
            budget.string(value)
            return value
        if tag == "f" and type(value) is str and _HEX_FLOAT.fullmatch(value):
            number = struct.unpack(">d", bytes.fromhex(value))[0]
            if not math.isfinite(number):
                raise CodecError("non-finite float is not permitted")
            has_float = True
            return number
        if tag not in ("a", "o") or type(value) is not int or value < 0:
            raise CodecError("invalid type tag or tag payload")
        if value > limits.max_nodes - budget.nodes:
            raise CodecError("container count exceeds remaining node budget")
        if tag == "a":
            return [visit(depth + 1) for _ in range(value)]
        result: dict[str, Any] = {}
        previous: str | None = None
        for _ in range(value):
            key = take()
            if type(key) is not str or (previous is not None and key <= previous):
                raise CodecError("object keys must be unique and sorted")
            budget.node(depth)
            budget.string(key)
            previous = key
            result[key] = visit(depth + 1)
        return result

    result = visit(0)
    if index != len(tokens):
        raise CodecError("trailing token stream data")
    if not has_float:
        raise CodecError("v1 encoding must contain a float")
    return result


def decode_payload(
    payload: bytes | str, *, encoding: str | None = None,
    authenticated_record: bool = False, limits: Limits = DEFAULT_LIMITS,
) -> Any:
    """Decode data only after the enclosing marker is authenticated.

    With no marker, tag-shaped objects/arrays remain ordinary legacy JSON.
    authenticated_record is a TRUSTED CALLER ASSERTION, not a wire field.
    Prefer decode_record rather than calling this on external material.
    """
    if encoding is not None and (
        encoding != VERSION or authenticated_record is not True
    ):
        raise CodecError("unsupported or unauthenticated encoding marker")
    raw = _raw(payload, limits)
    parsed = _parse(raw, limits)
    if encoding is None:
        result, _ = _normalize(parsed, limits)
        return result
    result = _untokens(parsed, limits)
    # Require one exact spelling for the extended wire representation.
    if _canonical(parsed, limits) != raw:
        raise CodecError("non-canonical extended encoding")
    return result


def clone_payload(value: Any, *, limits: Limits = DEFAULT_LIMITS) -> Any:
    """Return a validated independent in-memory snapshot without decoding tags."""
    # A freshly encoded local value needs no external marker authentication.
    encoded = encode_payload(value, limits=limits)
    return decode_payload(
        encoded.payload, encoding=encoded.encoding,
        authenticated_record=True, limits=limits,
    )


def _validated_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    if type(paths) is not tuple or len(paths) > 4096:
        raise CodecError("paths must be a bounded trusted tuple")
    seen: list[Path] = []
    for path in paths:
        if type(path) is not tuple or not path or len(path) > 64:
            raise CodecError("invalid value path")
        if path[0] == RECORD_MARKER or any(
            type(part) not in (str, int) or (type(part) is int and part < 0)
            for part in path
        ):
            raise CodecError("invalid value path component")
        if any(path[:len(old)] == old or old[:len(path)] == path for old in seen):
            raise CodecError("value paths cannot overlap")
        seen.append(path)
    return tuple(seen)


def _parent(record: dict[str, Any], path: Path) -> tuple[Any, str | int]:
    current: Any = record
    for part in path[:-1]:
        current = _child(current, part)
    _child(current, path[-1])
    return current, path[-1]


def _child(current: Any, part: str | int) -> Any:
    if type(current) is dict and type(part) is str and part in current:
        return current[part]
    if type(current) is list and type(part) is int and 0 <= part < len(current):
        return current[part]
    raise CodecError("approved value path does not exist")


def _wire_record(
    record: dict[str, Any], paths: tuple[Path, ...], limits: Limits,
) -> tuple[bytes, bool]:
    paths = _validated_paths(paths)
    if type(record) is not dict or RECORD_MARKER in record:
        raise CodecError("record root must be runtime-owned without reserved marker")
    # Clone through the data validator first, then enforce strictness on every
    # unselected field after replacing approved float-bearing data fields.
    result, _ = _normalize(record, limits)
    encoded_paths: list[list[str | int]] = []
    for path in paths:
        parent, key = _parent(result, path)
        encoded = encode_payload(parent[key], limits=limits)
        if encoded.encoding is not None:
            parent[key] = _parse(encoded.payload, limits)
            encoded_paths.append(list(path))
    if encoded_paths:
        result[RECORD_MARKER] = {"version": VERSION, "paths": encoded_paths}
    _, has_float = _normalize(result, limits, wire_paths=paths)
    if has_float:
        raise CodecError("float outside approved data fields")
    return _canonical(result, limits), bool(encoded_paths)


def digest_record(
    record: dict[str, Any], *, value_paths: tuple[Path, ...],
    limits: Limits = DEFAULT_LIMITS,
) -> str:
    """Hash a mixed envelope, admitting floats only in explicit data fields."""
    raw, extended = _wire_record(record, value_paths, limits)
    preimage = RECORD_DOMAIN + raw if extended else raw
    return "sha256:" + hashlib.sha256(preimage).hexdigest()


def encode_record(
    record: dict[str, Any], *, value_paths: tuple[Path, ...], seal_key: bytes,
    limits: Limits = DEFAULT_LIMITS,
) -> tuple[bytes, str]:
    """Encode selected data fields and authenticate marker plus whole envelope."""
    raw, _ = _wire_record(record, value_paths, limits)
    return raw, hmac.new(seal_key, raw, hashlib.sha256).hexdigest()


def decode_record(
    payload: bytes | str, seal: str, *, value_paths: tuple[Path, ...] | Callable[[dict[str, Any]], tuple[Path, ...]],
    seal_key: bytes, limits: Limits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """Authenticate before parsing marker; decode only caller-allowlisted fields."""
    raw = _raw(payload, limits)
    expected = hmac.new(seal_key, raw, hashlib.sha256).hexdigest()
    if (type(seal) is not str or re.fullmatch(r"[0-9a-f]{64}", seal) is None
            or not hmac.compare_digest(expected, seal)):
        raise CodecError("record authentication failed")
    parsed = _parse(raw, limits)
    if type(parsed) is not dict:
        raise CodecError("record root must be an object")
    # Resolve the trusted record-kind policy only AFTER authenticating bytes.
    # A callback may derive indices from the authenticated document shape; it
    # must never derive permission from the encoding marker itself.
    paths = _validated_paths(
        value_paths(parsed) if callable(value_paths) else value_paths
    )
    result, _ = _normalize(parsed, limits, wire_paths=paths)
    if RECORD_MARKER not in result:
        return _normalize(result, limits)[0]
    marker = result.pop(RECORD_MARKER)
    if type(marker) is not dict or set(marker) != {"version", "paths"}:
        raise CodecError("malformed record encoding marker")
    marked = marker["paths"]
    if (marker["version"] != VERSION or type(marked) is not list or not marked
            or any(type(path) is not list for path in marked)):
        raise CodecError("unsupported record encoding marker")
    marked_paths = _validated_paths(tuple(tuple(path) for path in marked))
    if any(path not in paths for path in marked_paths):
        raise CodecError("encoding marker selects an authority or unknown field")
    for path in marked_paths:
        parent, key = _parent(result, path)
        parent[key] = decode_payload(
            _canonical(parent[key], limits), encoding=VERSION,
            authenticated_record=True, limits=limits,
        )
    # Re-encoding enforces path order, marker completeness and one wire spelling.
    expected_raw, _ = _wire_record(result, paths, limits)
    if expected_raw != raw:
        raise CodecError("non-canonical extended record")
    return result
