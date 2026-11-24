"""Public connection-status projection for the exact finite HTTP endpoint."""

from __future__ import annotations

import math
from typing import Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

_MAX_SAFE_INTEGER = 2**53 - 1
_PROVIDER_FIELDS = (
    "provider_instance_id",
    "display_name",
    "enabled",
    "credential_status",
    "health_status",
    "reachability",
    "observed_at",
)
# The registry resource also serves internal model-search consumers. Its adapter
# identity belongs there; this endpoint exposes only the established UI shape.
PROVIDER_CONNECTION_STATUS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["revision", "providers"],
    "properties": {
        "revision": {"type": "integer", "minimum": 0, "maximum": _MAX_SAFE_INTEGER},
        "providers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(_PROVIDER_FIELDS),
                "properties": {
                    "provider_instance_id": {"type": "string", "minLength": 1},
                    "display_name": {"type": "string", "minLength": 1},
                    "enabled": {"type": "boolean"},
                    "credential_status": {
                        "enum": [
                            "configured",
                            "missing",
                            "not_required",
                        ]
                    },
                    "health_status": {"enum": ["verified", "unverified"]},
                    "reachability": {
                        "enum": [
                            "available",
                            "unavailable",
                            "unknown",
                        ]
                    },
                    "observed_at": {
                        "type": ["number", "null"],
                        "minimum": 0,
                        "maximum": _MAX_SAFE_INTEGER,
                    },
                    "catalog_provider_id": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}
_VALIDATOR = Draft202012Validator(PROVIDER_CONNECTION_STATUS_SCHEMA)


def present_provider_connection_status(
    result: Mapping[str, object],
) -> dict[str, object]:
    """Validate public facts and omit internal routing and credential material.

    Credential presence is kept separate from verified reachability. Invalid
    public fields fail the request instead of becoming an empty success.
    """
    revision = result.get("revision")
    providers = result.get("providers")
    if (
        type(revision) is not int
        or not 0 <= revision <= _MAX_SAFE_INTEGER
        or not isinstance(providers, list)
    ):
        raise ValueError("provider connection status is invalid")
    projected: list[dict[str, object]] = []
    for provider in providers:
        if not isinstance(provider, Mapping):
            raise ValueError("provider connection status is invalid")
        item = {key: provider[key] for key in _PROVIDER_FIELDS if key in provider}
        if "catalog_provider_id" in provider:
            item["catalog_provider_id"] = provider["catalog_provider_id"]
        for key in ("provider_instance_id", "display_name", "catalog_provider_id"):
            if key in item and (not isinstance(item[key], str) or not item[key].strip()):
                raise ValueError("provider connection status is invalid")
        observed_at = item.get("observed_at")
        if observed_at is not None and (
            type(observed_at) not in (int, float)
            or not 0 <= observed_at <= _MAX_SAFE_INTEGER
            or not math.isfinite(observed_at)
        ):
            raise ValueError("provider connection status is invalid")
        projected.append(item)
    public = {"revision": revision, "providers": projected}
    try:
        _VALIDATOR.validate(public)
    except ValidationError:
        # Validation errors can quote source values. Never expose those details.
        raise ValueError("provider connection status is invalid") from None
    return public
