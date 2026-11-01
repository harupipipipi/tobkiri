"""Finite ordinary PackVM application data on strict-canonical wire channels.

The versioned fields below carry no authority. Request owners preserve their
existing protected channel/launch checks; request_digest is not a guest-side
cryptographic binding of these fields. Child stdout remains untrusted. Only
Host callers that verified both helper HMAC and guest signature may decode a
public terminal. Bridge and saved continuation protocols are not adapted here.
"""
from __future__ import annotations

from typing import Any

from .data_codec import (
    VERSION,
    CodecError,
    Limits,
    _canonical,
    decode_data_tokens,
    decode_payload,
    encode_payload,
)
from .canonical import strict_loads

INVOKE_RESULT_KIND = "tobkiri.packvm.invoke.result.v1"
ENCODED_INVOKE_RESULT_KIND = "tobkiri.packvm.invoke.result.v2"
PRIVATE_RESULT_KIND = "tobkiri.packvm.private.result.v1"
PAYLOAD_FIELDS = frozenset({"payload", "payload_encoding", "payload_tokens"})
# Logical data must fit the strict profile *with* its largest enclosing wire
# record: request.payload is two levels; helper.payload.data.outcome is three.
# Flattening tokens must not permit deeper data than the legacy envelopes.
REQUEST_LIMITS = Limits(max_bytes=1280 * 1024, max_depth=62)
RESULT_LIMITS = Limits(max_bytes=16 * 1024 * 1024, max_depth=61)


def _object(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise CodecError("PackVM application data must be an object")
    return value


def _terminal(value: Any) -> dict[str, Any]:
    result = _object(value)
    kind = result.get("kind")
    if isinstance(kind, str) and kind.startswith("tobkiri.packvm."):
        raise CodecError("PackVM control frame is not a terminal outcome")
    return result


def _tokens(value: Any, encoding: Any, limits: Limits) -> dict[str, Any]:
    # Serialization is byte-bounded even for malformed carrier objects. The
    # codec then enforces the flat token grammar and logical depth/node bounds.
    return _object(decode_data_tokens(
        _canonical(value, limits), encoding=encoding, limits=limits,
    ))


def encode_invoke_payload(value: Any) -> dict[str, Any]:
    """Encode an already-admitted logical request, without changing its digest."""
    encoded = encode_payload(_object(value), limits=REQUEST_LIMITS)
    if encoded.encoding is None:
        return {"payload": strict_loads(encoded.payload, max_bytes=REQUEST_LIMITS.max_bytes)}
    return {
        "payload_encoding": VERSION,
        "payload_tokens": strict_loads(encoded.payload, max_bytes=REQUEST_LIMITS.max_bytes),
    }


def decode_invoke_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Validate one exact request field variant and return ordinary Pack data."""
    fields = set(record) & PAYLOAD_FIELDS
    if fields == {"payload"}:
        # Unmarked tag-shaped values remain ordinary data. Bare floats on this
        # path are forbidden: only the exact extended field variant opts in.
        return _object(decode_payload(
            _canonical(record["payload"], REQUEST_LIMITS), limits=REQUEST_LIMITS,
        ))
    if fields != {"payload_encoding", "payload_tokens"}:
        raise CodecError("PackVM invocation payload fields are invalid")
    return _tokens(record["payload_tokens"], record["payload_encoding"], REQUEST_LIMITS)


def validate_invoke_payload_fields(record: dict[str, Any]) -> dict[str, Any]:
    """Validate request data without promoting its carrier into authority."""
    decode_invoke_payload(record)
    return {key: record[key] for key in sorted(set(record) & PAYLOAD_FIELDS)}


def private_terminal(value: Any) -> dict[str, Any]:
    """Wrap terminal Pack data; the root must distrust and revalidate this."""
    encoded = encode_payload(_terminal(value), limits=RESULT_LIMITS)
    if encoded.encoding is None:
        return {"kind": INVOKE_RESULT_KIND, "outcome": value}
    return {
        "kind": PRIVATE_RESULT_KIND,
        "outcome_encoding": VERSION,
        "outcome_tokens": strict_loads(encoded.payload, max_bytes=RESULT_LIMITS.max_bytes),
    }


def public_terminal_from_child(record: dict[str, Any]) -> dict[str, Any]:
    """Validate untrusted child output, then synthesize the public envelope."""
    if set(record) == {"kind", "outcome"} and record["kind"] == INVOKE_RESULT_KIND:
        outcome = _terminal(decode_payload(
            _canonical(record["outcome"], RESULT_LIMITS), limits=RESULT_LIMITS,
        ))
        return {"kind": INVOKE_RESULT_KIND, "outcome": outcome}
    if (set(record) != {"kind", "outcome_encoding", "outcome_tokens"}
            or record.get("kind") != PRIVATE_RESULT_KIND):
        raise CodecError("PackVM private terminal fields are invalid")
    outcome = _terminal(_tokens(
        record["outcome_tokens"], record["outcome_encoding"], RESULT_LIMITS,
    ))
    # Re-encoding is bounded and supplies our own version and token stream;
    # no artifact-authored public marker or authority field is forwarded.
    encoded = encode_payload(outcome, limits=RESULT_LIMITS)
    return {
        "kind": ENCODED_INVOKE_RESULT_KIND,
        "outcome_encoding": VERSION,
        "outcome_tokens": strict_loads(encoded.payload, max_bytes=RESULT_LIMITS.max_bytes),
    }


def decode_invoke_outcome(record: dict[str, Any]) -> dict[str, Any]:
    """Decode a terminal only AFTER the Host verifies HMAC and guest signature."""
    if set(record) == {"kind", "outcome"} and record["kind"] == INVOKE_RESULT_KIND:
        return _terminal(decode_payload(
            _canonical(record["outcome"], RESULT_LIMITS), limits=RESULT_LIMITS,
        ))
    if (set(record) != {"kind", "outcome_encoding", "outcome_tokens"}
            or record.get("kind") != ENCODED_INVOKE_RESULT_KIND
            or record.get("outcome_encoding") != VERSION):
        raise CodecError("PackVM invocation result fields are invalid")
    return _terminal(decode_payload(
        _canonical(record["outcome_tokens"], RESULT_LIMITS),
        encoding=record["outcome_encoding"], authenticated_record=True,
        limits=RESULT_LIMITS,
    ))
