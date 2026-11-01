"""Bounded, identity-bound read-back receipts for Profile activation."""

from __future__ import annotations

from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest, canonical_json


_DATA_FIELDS = {
    "profile": ("profile_id", "profile_revision", "catalog_revision"),
    "profile_lock": (
        "lock_digest",
        "plan_digest",
        "bundle_digest",
        "closure_digest",
        "security_epoch",
        "profile_authority_snapshot_digest",
    ),
    "resolved_plan": ("plan_digest", "bundle_digest", "closure_digest"),
    "authority_snapshot": (
        "profile_authority_snapshot_digest",
        "security_epoch",
        "fencing_token",
    ),
}
MAX_ACTIVATION_RECEIPT_BYTES = 64 * 1024


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Profile activation read-back is unavailable")
    return value


def bounded_activation_receipt(
    result: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    """Keep canonical activation proof while omitting graph presentation data.

    The complete Profile graph remains available from ``profile.read``. A
    mutation receipt retains its immutable record references and the scalar
    joins needed to validate the exact activation, rather than duplicating the
    full graph in the durable operation journal.
    """

    data = _mapping(snapshot.get("data"))
    activation = dict(_mapping(data.get("activation_record")))
    records = _mapping(snapshot.get("records"))
    if (
        result.get("state") != "active"
        or snapshot.get("surface") != "profile"
        or snapshot.get("state") != "ready"
        or result.get("runtime_surface_api_version")
        != snapshot.get("runtime_surface_api_version")
        or any(
            result.get(key) != activation.get(key)
            for key in (
                "profile_id",
                "activation_id",
                "plan_digest",
                "security_epoch",
                "fencing_token",
            )
        )
        or any(
            snapshot.get(key) != activation.get(key)
            for key in (
                "profile_id",
                "profile_revision",
                "plan_digest",
                "catalog_revision",
            )
        )
        or _mapping(records.get("activation_record")).get("digest")
        != canonical_digest(activation)
    ):
        raise ValueError("Profile activation read-back binding changed")
    compact_data = {
        name: {key: _mapping(data.get(name))[key] for key in keys}
        for name, keys in _DATA_FIELDS.items()
    }
    compact_data["activation_record"] = activation
    receipt = {
        **dict(result),
        "authoritative_snapshot": {**dict(snapshot), "data": compact_data},
    }
    if len(canonical_json(receipt)) > MAX_ACTIVATION_RECEIPT_BYTES:
        raise ValueError("Profile activation receipt exceeds its finite bound")
    return receipt
