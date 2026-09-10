"""Strict v2 request framing, not a substitute for signed transport or Broker.

The saved guest dispatcher uses this codec; existing v1 guest/Host dispatch is unchanged.
Expected identities, stage, predecessor and target must come from authenticated
Host state, never from the frame being checked.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.saved_tools import MAX_SAVED_TOOL_HOPS

from .continuation_chain import ChainIdentity

_FIELDS = {
    "kind",
    "version",
    "request_id",
    "binding_digest",
    "hop",
    "nonce",
    "previous_digest",
    "target",
    "payload",
    "state",
}
_NONCE = re.compile(r"[0-9a-f]{48}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class ValidatedContinuation:
    """Immutable encoded data after identity and strict-JSON validation."""

    identity: ChainIdentity
    hop: int
    nonce: str
    frame: bytes
    digest: str
    payload: bytes
    state: bytes


@dataclass(frozen=True)
class ValidatedResult:
    """Bounded encoded result matched to one validated request, not a grant."""

    frame: bytes
    digest: str


def seal_continuation_intent(
    encoded: bytes,
    *,
    identity: ChainIdentity,
    hop: int,
    previous_digest: str | None,
    target: tuple[str, str],
    nonce: str,
    max_hops: int = 4,
) -> ValidatedContinuation:
    """Add root-owned framing to an application intent, without authorizing it.

    All keyword arguments come from the authenticated execution boundary, not
    the untrusted intent. Register the returned frame before its first effect;
    the chain ledger and Authority/Broker still enforce lifetime and execution.
    """
    if type(encoded) is not bytes:
        raise ValueError("continuation intent must be encoded bytes")
    value = strict_loads(encoded, max_bytes=60 * 1024, max_depth=16)
    if (
        not isinstance(value, dict)
        or set(value) != {"kind", "hop", "target", "payload", "state"}
        or value["kind"] != "tobkiri.packvm.continuation.intent.v2"
        or type(value["hop"]) is not int
        or value["hop"] != hop
        or value["target"] != {"contract_id": target[0], "operation_id": target[1]}
    ):
        raise ValueError("continuation intent does not match the captured step")
    frame = {
        "kind": "tobkiri.packvm.continuation.request.v2",
        "version": 2,
        "request_id": identity.request_id,
        "binding_digest": identity.binding_digest,
        "hop": hop,
        "nonce": nonce,
        "previous_digest": previous_digest,
        "target": value["target"],
        "payload": value["payload"],
        "state": value["state"],
    }
    return validate_continuation_request(
        canonical_json(frame), identity=identity, hop=hop,
        previous_digest=previous_digest, target=target, max_hops=max_hops,
    )


def validate_continuation_result(
    encoded: bytes,
    *,
    request: ValidatedContinuation,
) -> ValidatedResult:
    """Reject swapped results and ambiguous success/error envelopes."""
    if type(encoded) is not bytes:
        raise ValueError("continuation result must be encoded bytes")
    value = strict_loads(encoded, max_bytes=512 * 1024, max_depth=16)
    if (
        not isinstance(value, dict)
        or set(value) != {"kind", "version", "request_digest", "outcome"}
        or value["kind"] != "tobkiri.packvm.continuation.result.v2"
        or type(value["version"]) is not int
        or value["version"] != 2
        or value["request_digest"] != request.digest
    ):
        raise ValueError("continuation result binding is invalid")
    outcome = value["outcome"]
    if not isinstance(outcome, dict):
        raise ValueError("continuation outcome is invalid")
    if outcome.get("status") == "ok":
        valid = set(outcome) == {"status", "value"} and isinstance(outcome["value"], dict)
    elif outcome.get("status") == "error":
        error = outcome.get("error")
        valid = (
            set(outcome) == {"status", "error"}
            and isinstance(error, dict)
            and set(error) == {"code", "message"}
            and isinstance(error["code"], str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", error["code"]) is not None
            and isinstance(error["message"], str)
            and len(error["message"]) <= 512
        )
    else:
        valid = False
    if not valid:
        raise ValueError("continuation outcome is invalid")
    return ValidatedResult(canonical_json(value), canonical_digest(value))


def validate_continuation_request(
    encoded: bytes,
    *,
    identity: ChainIdentity,
    hop: int,
    previous_digest: str | None,
    target: tuple[str, str],
    max_hops: int = 4,
) -> ValidatedContinuation:
    """Validate one bounded request against independently captured expectations.

    Payload schema checks, resource scope, target readiness and execution remain
    the caller's Authority/Broker responsibilities. No effect is performed here.
    """
    if type(encoded) is not bytes:
        raise ValueError("continuation request must be encoded bytes")
    if (type(max_hops) is not int or not 1 <= max_hops <= MAX_SAVED_TOOL_HOPS
            or type(hop) is not int or not 0 <= hop < max_hops):
        raise ValueError("expected continuation hop is invalid")
    if (hop == 0 and previous_digest is not None) or (
        hop > 0
        and (not isinstance(previous_digest, str) or _DIGEST.fullmatch(previous_digest) is None)
    ):
        raise ValueError("expected continuation predecessor is invalid")
    value = strict_loads(encoded, max_bytes=64 * 1024, max_depth=16)
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("continuation request fields are invalid")
    if (
        value["kind"] != "tobkiri.packvm.continuation.request.v2"
        or type(value["version"]) is not int
        or value["version"] != 2
        or value["request_id"] != identity.request_id
        or value["binding_digest"] != identity.binding_digest
        or type(value["hop"]) is not int
        or value["hop"] != hop
        or value["previous_digest"] != previous_digest
        or value["target"] != {"contract_id": target[0], "operation_id": target[1]}
        or not isinstance(value["nonce"], str)
        or _NONCE.fullmatch(value["nonce"]) is None
        or not isinstance(value["payload"], dict)
        or not isinstance(value["state"], dict)
    ):
        raise ValueError("continuation request binding is invalid")
    frame = canonical_json(value)
    return ValidatedContinuation(
        identity,
        hop,
        value["nonce"],
        frame,
        canonical_digest(value),
        canonical_json(value["payload"]),
        canonical_json(value["state"]),
    )
