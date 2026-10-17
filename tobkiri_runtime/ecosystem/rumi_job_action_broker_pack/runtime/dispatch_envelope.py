"""Immutable dispatch identity retained by the public job owner, never authority."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

VERSION = "tobkiri.job-dispatch-envelope.v1"
FIELDS = frozenset(
    {"profile_id", "action_id", "payload", "idempotency_key", "schedule_id", "lease_id"}
)
PENDING = frozenset(
    {
        "accepted",
        "running",
        "waiting",
        "waiting_approval",
        "reconciliation_required",
        "cancellation_pending",
        "unknown",
        "unavailable",
    }
)
TERMINAL = frozenset({"ok", "completed", "failed", "cancelled"})


def immutable_json(value: Any) -> Any:
    """Copy finite JSON before persistence so caller mutations cannot rebind it."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > 128 * 1024:
        raise ValueError("job dispatch envelope exceeds its bounded budget")
    return json.loads(encoded)


def envelope_digest(value: Mapping[str, Any]) -> str:
    """Bind the exact Profile, payload, idempotency and scheduler lease identity."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def restored_envelope(
    entry: Mapping[str, Any], *, profile_id: str, key: str
) -> dict[str, Any] | None:
    """Verify retained identity; incomplete historical records never imply replay."""
    value = entry.get("dispatch_envelope")
    digest = entry.get("dispatch_envelope_digest")
    if value is None or digest is None:
        return None
    if not isinstance(value, Mapping) or set(value) != FIELDS | {"version"}:
        raise PermissionError("retained job envelope is invalid")
    envelope = immutable_json(value)
    if (
        envelope["version"] != VERSION
        or envelope["profile_id"] != profile_id
        or envelope["idempotency_key"] != key
        or envelope["action_id"] != entry.get("action_id")
        or envelope["idempotency_key"] != entry.get("idempotency_key")
        or not isinstance(envelope["payload"], dict)
        or envelope_digest(envelope) != digest
    ):
        raise PermissionError("retained job envelope binding changed")
    return {field: envelope[field] for field in FIELDS}


def retained_status(result: Any) -> str:
    """Preserve owner waits and ambiguity instead of inventing failure/completion."""
    if not isinstance(result, Mapping):
        return "reconciliation_required"
    status = result.get("status")
    return (
        status
        if isinstance(status, str) and status in PENDING | TERMINAL
        else "reconciliation_required"
    )
