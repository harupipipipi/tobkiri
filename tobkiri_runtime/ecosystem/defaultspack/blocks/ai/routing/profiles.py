"""Approved finite compatibility adapter over the model profile owner."""

from __future__ import annotations

from typing import Any, Mapping

from blocks._common import error, ok
from blocks.coding._approval import (
    approval_invalid_response,
    approval_required,
    is_server_approved,
)
from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import invoke_global_contract

_RESOURCE = "rumi.resource.ai.model.profile.v1"
_MANAGE = "rumi.action.ai.model.profile.manage.v1"


def run(input_data, context):
    """Expose the finite legacy profile route through the canonical owner."""
    data = dict(input_data or {})
    method = str(data.get("_method") or "GET").upper()
    if method == "GET":
        snapshot = _invoke(_RESOURCE, "list", {})
        return ok(
            {
                "profiles": snapshot.get("profiles", []),
                "revision": snapshot.get("revision", 0),
            }
        )
    operation = f"ai.model_profile.{method.lower()}"
    invalid = approval_invalid_response(operation, data, error)
    if invalid is not None:
        return invalid
    if not is_server_approved(context, operation=operation, input_data=data):
        return ok(approval_required(operation, "high", args=data))
    try:
        snapshot = _invoke(_RESOURCE, "list", {})
        revision = int(snapshot.get("revision") or 0)
        if method == "DELETE":
            profile_id = str(data.get("name") or data.get("key") or "")
            result = _invoke(
                _MANAGE,
                "delete",
                {
                    "model_profile_id": profile_id,
                    "expected_revision": revision,
                },
            )
            return ok(result)
        if method not in {"POST", "PUT"}:
            return error("unsupported method", "INVALID_METHOD")
        profile_id = str(data.get("name") or data.get("key") or "").strip()
        supplied = data.get("profile") if method == "POST" else data.get("updates")
        if not profile_id or not isinstance(supplied, Mapping):
            return error("profile key and object are required", "MISSING_PARAM")
        record = _existing(snapshot, profile_id) if method == "PUT" else {}
        record.update(dict(supplied))
        provider_id = str(record.pop("provider", "") or "")
        model_id = str(record.get("model_id") or "")
        if provider_id and model_id and not model_id.startswith(f"{provider_id}/"):
            model_id = f"{provider_id}/{model_id}"
        requirements = record.get("requirements")
        requirements = dict(requirements) if isinstance(requirements, Mapping) else {}
        if provider_id:
            requirements.setdefault("preferred_provider_id", provider_id)
        result = _invoke(
            _MANAGE,
            "save",
            {
                "expected_revision": revision,
                "record": {
                    "model_profile_id": profile_id,
                    "display_name": str(
                        record.get("display_name")
                        or record.get("name")
                        or profile_id
                    ),
                    "model_id": model_id,
                    "requirements": requirements,
                    "credential_handle": record.get("credential_handle"),
                    "parameters": record.get("parameters", {}),
                    "enabled": bool(record.get("enabled", True)),
                    "metadata": {"legacy_profile": True},
                },
            },
        )
        return ok(result)
    except (KeyError, RuntimeError, ValueError) as exc:
        return error(type(exc).__name__, "MODEL_PROFILE_FAILED")


def _existing(snapshot: Mapping[str, Any], profile_id: str) -> dict[str, Any]:
    """Return one owner snapshot record for a legacy update."""
    profiles = snapshot.get("profiles")
    profiles = profiles if isinstance(profiles, list) else []
    for item in profiles:
        if isinstance(item, Mapping) and item.get("model_profile_id") == profile_id:
            return dict(item)
    raise KeyError("model profile is unknown")


def _invoke(
    contract_id: str,
    operation: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Invoke the selected model-profile owner through the global registry."""
    registry = get_container().get_or_none("v4_dispatch_session")
    if registry is None:
        raise RuntimeError("model profile owner is unavailable")
    value = invoke_global_contract(registry, contract_id, operation, dict(payload))
    if not isinstance(value, dict):
        raise RuntimeError("model profile owner returned an invalid result")
    return value
