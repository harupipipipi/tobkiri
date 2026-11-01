"""Pure frozen-payload validation for the existing model-access PendingEffect."""

from __future__ import annotations

from typing import Any, Mapping
from tobkiri_protocol.canonical import canonical_digest

_FIELDS = {
    "version",
    "profile_id",
    "provider_instance_id",
    "expected_revision",
    "model_access",
    "native_capability_revision",
    "request_digest",
    "plan_digest",
}
_REQUEST_FIELDS = {"profile_id", "provider_instance_id", "expected_revision", "model_access"}


def model_access_execute_payload(
    request: Mapping[str, Any], plan: Mapping[str, Any]
) -> dict[str, Any]:
    """Freeze scope and both digests without reading state or granting approval."""
    if (
        set(request) != _REQUEST_FIELDS
        or set(plan) != _FIELDS
        or plan.get("version") != "tobkiri.provider-model-access.plan.v1"
        or any(
            plan.get(key) != request.get(key)
            for key in ("profile_id", "provider_instance_id", "expected_revision")
        )
        or any(
            not isinstance(plan.get(key), str) or not plan[key].strip()
            for key in ("profile_id", "provider_instance_id")
        )
        or type(plan.get("expected_revision")) is not int
        or not 0 <= plan["expected_revision"] <= 2**53 - 1
        or not isinstance(plan.get("model_access"), Mapping)
        or plan.get("request_digest") != canonical_digest(dict(request))
        or plan.get("plan_digest")
        != canonical_digest({key: value for key, value in plan.items() if key != "plan_digest"})
    ):
        raise ValueError("model access effect plan is invalid")
    return {"request": dict(request), "plan": dict(plan)}
