"""Canonical Capability Plan validation shared by Host-owned compilers."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Mapping


CAPABILITY_PLAN_SCHEMA_VERSION = "tobkiri.capability-plan/v1"


class CapabilityPlanValidationError(ValueError):
    """Raised when a Capability Plan is not canonical or digest-bound."""


def canonical_capability_plan_digest(plan: Mapping[str, Any]) -> str:
    """Hash the complete canonical payload, excluding only its digest."""

    payload = {
        str(key): value
        for key, value in plan.items()
        if str(key) != "digest"
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def validate_capability_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached canonical plan or fail closed.

    Consumers must read authority only from ``effective_capabilities`` and
    ``provider_selections``. Legacy aliases are deliberately ignored.
    """

    payload = json.loads(
        json.dumps(dict(plan), ensure_ascii=False, default=str)
    )
    if payload.get("schema_version") != CAPABILITY_PLAN_SCHEMA_VERSION:
        raise CapabilityPlanValidationError(
            "Capability Plan schema_version must be v1"
        )
    if not str(payload.get("plan_id") or "").strip():
        raise CapabilityPlanValidationError(
            "Capability Plan plan_id is required"
        )
    if not str(payload.get("registry_revision") or "").strip():
        raise CapabilityPlanValidationError(
            "Capability Plan registry_revision is required"
        )
    digest = str(payload.get("digest") or "").strip()
    expected = canonical_capability_plan_digest(payload)
    if not digest or digest != expected:
        raise CapabilityPlanValidationError(
            "Capability Plan digest does not match canonical payload"
        )
    effective = payload.get("effective_capabilities")
    if not isinstance(effective, list) or any(
        not isinstance(value, str) or not value.strip()
        for value in effective
    ):
        raise CapabilityPlanValidationError(
            "Capability Plan effective_capabilities is invalid"
        )
    if effective != sorted(set(effective)):
        raise CapabilityPlanValidationError(
            "Capability Plan effective_capabilities must be canonical"
        )
    selections = payload.get("provider_selections")
    if not isinstance(selections, Mapping):
        raise CapabilityPlanValidationError(
            "Capability Plan provider_selections is required"
        )
    for contract_id, provider_ids in selections.items():
        if (
            not isinstance(contract_id, str)
            or not contract_id.strip()
            or not isinstance(provider_ids, list)
            or any(
                not isinstance(value, str) or not value.strip()
                for value in provider_ids
            )
            or provider_ids != sorted(set(provider_ids))
        ):
            raise CapabilityPlanValidationError(
                "Capability Plan provider_selections is invalid"
            )
    return payload
