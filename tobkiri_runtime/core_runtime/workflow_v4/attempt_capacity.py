"""Size-only lifecycle envelopes for the shared encrypted Workflow journal.

These projections are never persisted and never replace an executable payload.
The journal still retains every full record and permanent replay fence. This
reserves completion space; it does not solve lifetime journal exhaustion.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping

from tobkiri_host.interactive_effects import _PendingEffect

# Maximum remaining successful CAS transitions through the existing one-shot
# lifecycles. Terminal rows do not grow again. Recovery/cancellation shorten
# these paths; polling does not advance a revision.
# Attempt: preparing -> pending/reserved -> claimed -> dispatched -> terminal.
_ATTEMPT_REMAINING = {
    "preparing": 4,
    "pending": 3,
    "reserved": 3,
    "claimed": 2,
    "dispatched": 1,
}
# Pending: prepared -> approval_pending -> approved -> claimed -> dispatched
# -> terminal. The Workflow controller does not use policy-derived grants.
_PENDING_REMAINING = {
    "prepared": 5,
    "approval_pending": 4,
    "approved": 3,
    "claimed": 2,
    "dispatched": 1,
}
_OUTCOME_DIGEST = "sha256:" + "0" * 64
# _new_effect_identifiers uses token_hex(18), with this exact prefix.
_PENDING_EFFECT_ID = "pending-effect-" + "0" * 36
# JSON's finite binary64 representation uses at most 24 ASCII characters:
# sign + 17 significant digits + decimal point + e + exponent sign + 3 digits.
# The Workflow controller clock is coerced to float before creating/updating
# records. The value here is a size-only placeholder, never a real timestamp.
_WIDEST_TIMESTAMP = -sys.float_info.max


def valid_outcome_digest(value: object) -> bool:
    """Recognize fixed-width canonical terminal evidence before any writes."""
    return (
        isinstance(value, str)
        and len(value) == len(_OUTCOME_DIGEST)
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _size(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _row_headroom(row: Mapping[str, Any], *, pending: bool) -> int:
    payload = row["payload"]
    remaining = (_PENDING_REMAINING if pending else _ATTEMPT_REMAINING).get(
        payload["state"], 0
    )
    if not remaining:
        return 0
    if pending:
        # Account for the exact controller round trip too: old optional fields
        # can be filled, and integer created_at/expires_at become float values.
        # This validation/projection does not rewrite the authenticated record.
        future = _PendingEffect.from_dict(payload).to_dict()
        future["state"] = "approval_pending"  # Longest pending lifecycle state.
        future["updated_at"] = _WIDEST_TIMESTAMP
    else:
        future = dict(payload)
        future["state"] = "dispatched"  # Longest attempt lifecycle state.
        if (
            future.get("authority_mode") == "interactive_only"
            and future.get("effect_id") is None
        ):
            future["effect_id"] = _PENDING_EFFECT_ID
    future["outcome_digest"] = _OUTCOME_DIGEST
    if not pending and "outcome_digest" in payload:
        # Older/nonstandard active attempt evidence is retained by intermediate
        # attempt CAS; never discard its width when reserving lifecycle growth.
        future["outcome_digest"] = max(
            (payload["outcome_digest"], _OUTCOME_DIGEST), key=_size
        )
    projected = dict(row)
    projected.update(revision=row["revision"] + remaining, payload=future)
    return max(0, _size(projected) - _size(row))


def completion_headroom(document: Mapping[str, Any]) -> int:
    """Count all active rows' maximum remaining serialized lifecycle growth.

    Summing both namespaces retains the attempt allowance while its related
    pending row finishes first, and protects other active work during admission.
    Keys/container punctuation already exist in the actual document size.
    A new pending row must pass its own atomic capacity check before approval
    opens or a provider can run; a rejected prepare leaves the attempt fence.
    """
    return sum(
        _row_headroom(row, pending=namespace == "pending")
        for namespace in ("attempts", "pending")
        for row in document[namespace].values()
    )
