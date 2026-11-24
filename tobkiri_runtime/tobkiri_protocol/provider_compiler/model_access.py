"""Connection-bound model restrictions, independent of keys and catalog snapshots."""

from __future__ import annotations

import re
from typing import Any, Mapping

from .native_filters import (
    apply_native_routing, is_official_openrouter, normalize_native_filters,
)

VERSION = "tobkiri.connection-model-access.v1"
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,511}$")
_RESERVED_PARAMETERS = {
    "model", "models", "provider", "route", "extra_body", "fallbacks",
    "provider_id", "provider_connection_id", "endpoint", "credential_handle",
}


def normalize_model_access(
    value: Any, *, connection: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate exact stable IDs and official filters for one saved connection."""
    if not isinstance(value, Mapping) or set(value) - {
        "version", "mode", "model_ids", "native_filters",
    } or value.get("version") != VERSION:
        raise ValueError("model access policy schema is invalid")
    mode = value.get("mode")
    if not isinstance(mode, str) or mode not in {"all", "explicit"}:
        raise ValueError("model access mode is invalid")
    ids = value.get("model_ids", [])
    if not isinstance(ids, list) or len(ids) > 4096 or any(
        not isinstance(item, str) or not _MODEL_ID.fullmatch(item) for item in ids
    ):
        raise ValueError("model access IDs are invalid")
    if mode == "all" and ids:
        raise ValueError("all model access must not pin catalog IDs")
    result: dict[str, Any] = {
        "version": VERSION, "mode": mode, "model_ids": sorted(set(ids)),
    }
    if value.get("native_filters") is not None:
        result["native_filters"] = normalize_native_filters(
            value["native_filters"],
            verified_openrouter=is_official_openrouter(connection),
        )
    return result


def effective_model_access(connection: Mapping[str, Any]) -> dict[str, Any] | None:
    """Preserve absent legacy policy and every explicit legacy list, including empty."""
    if "model_access" in connection:
        return normalize_model_access(connection["model_access"], connection=connection)
    metadata = connection.get("metadata")
    for source in (connection, metadata if isinstance(metadata, Mapping) else {}):
        if "allowed_models" in source:
            return normalize_model_access({
                "version": VERSION, "mode": "explicit",
                "model_ids": source["allowed_models"],
            }, connection=connection)
    return None


def model_is_allowed(connection: Mapping[str, Any], model_id: str) -> bool:
    """Restrict an existing authorized route; never establish its availability."""
    policy = effective_model_access(connection)
    return policy is None or policy["mode"] == "all" or model_id in policy["model_ids"]


def compile_connection_parameters(
    connection: Mapping[str, Any], model_id: str, parameters: Any,
) -> dict[str, Any]:
    """Deny unauthorized IDs before transport and prevent route/model overrides."""
    if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id):
        raise ValueError("provider model ID is invalid")
    if not model_is_allowed(connection, model_id):
        raise PermissionError("model is not permitted by this connection")
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, Mapping) or set(parameters) & _RESERVED_PARAMETERS:
        raise ValueError("request parameters cannot override the saved model route")
    result = dict(parameters)
    policy = effective_model_access(connection)
    native = policy.get("native_filters") if policy else None
    if native:
        return apply_native_routing(native, result)
    return result
