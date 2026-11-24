"""Finite, credential-free preparation and execution of model access policy."""

from __future__ import annotations

import re
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest

from .model_access import normalize_model_access
from .provider_filters import CAPABILITY_REVISION, is_official_openrouter
from .registry import ProviderRegistry, ProviderRegistryConflict

PREPARE_OPERATION = "rumi_provider_registry_pack.model-access-prepare"
EXECUTE_OPERATION = "rumi_provider_registry_pack.model-access-save"
PLAN_VERSION = "tobkiri.provider-model-access.plan.v1"
_FIELDS = {"profile_id", "provider_instance_id", "expected_revision", "model_access"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")


def prepare_model_access(
    registry: ProviderRegistry,
    request: Mapping[str, Any],
    *,
    native_capability_revision: str | None = None,
) -> dict[str, Any]:
    """Freeze public policy against the captured profile and exact revision.

    Capability evidence is supplied by the Host, never the public request.
    Preparation neither writes state nor invokes a credential or network client.
    """
    if (
        not isinstance(request, Mapping)
        or set(request) != _FIELDS
        or request["profile_id"] != registry.profile_id
        or not isinstance(request["provider_instance_id"], str)
        or not _IDENTIFIER.fullmatch(request["provider_instance_id"])
        or type(request["expected_revision"]) is not int
        or not 0 <= request["expected_revision"] <= 2**53 - 1
    ):
        raise ValueError("model access request is invalid")
    snapshot = registry.snapshot()
    if snapshot["revision"] != request["expected_revision"]:
        raise ProviderRegistryConflict("provider registry revision changed")
    matches = [
        item
        for item in snapshot["providers"]
        if item["provider_instance_id"] == request["provider_instance_id"]
    ]
    if len(matches) != 1:
        raise KeyError("provider connection is unavailable")
    connection = matches[0]
    capability = (
        CAPABILITY_REVISION
        if native_capability_revision == CAPABILITY_REVISION and is_official_openrouter(connection)
        else None
    )
    policy = normalize_model_access(request["model_access"], connection=connection)
    native = policy.get("native_filters")
    if native is not None and capability is None:
        raise PermissionError("provider native capability is unavailable")
    plan = {
        "version": PLAN_VERSION,
        "profile_id": registry.profile_id,
        "provider_instance_id": request["provider_instance_id"],
        "expected_revision": request["expected_revision"],
        "model_access": policy,
        "native_capability_revision": capability,
        "request_digest": canonical_digest(dict(request)),
    }
    return {**plan, "plan_digest": canonical_digest(plan)}


def execute_model_access(
    registry: ProviderRegistry,
    payload: Mapping[str, Any],
    *,
    native_capability_revision: str | None = None,
) -> dict[str, Any]:
    """Apply only the exact approved plan with the registry's atomic CAS.

    The caller must enter through the captured Host PendingEffect execution
    route. This pure service helper does not issue or bypass approval.
    """
    if not isinstance(payload, Mapping) or set(payload) != {"request", "plan"}:
        raise ValueError("model access execution is invalid")
    request, plan = payload["request"], payload["plan"]
    if not isinstance(request, Mapping) or not isinstance(plan, Mapping):
        raise ValueError("model access execution is invalid")
    expected = prepare_model_access(
        registry,
        request,
        native_capability_revision=native_capability_revision,
    )
    if canonical_digest(dict(plan)) != canonical_digest(expected):
        raise PermissionError("model access changed after preparation")
    saved = registry.set_model_access(
        expected["provider_instance_id"],
        expected["model_access"],
        expected_revision=expected["expected_revision"],
    )
    return {
        "profile_id": registry.profile_id,
        "provider_instance_id": expected["provider_instance_id"],
        "registry_revision": saved["store_revision"],
        "model_access": expected["model_access"],
        "native_capability": expected["native_capability_revision"],
    }
