"""Approved compatibility adapter for provider connections and credentials."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from blocks._common import error, ok
from blocks.coding._approval import (
    approval_invalid_response,
    approval_required,
    is_server_approved,
)
from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import invoke_global_contract
from domain.ai_client.api_key_store import set_provider_api_key
from domain.ai_client.model_availability import ModelAvailabilityService

_CREDENTIAL_MANAGE = "rumi.action.credential.manage.v1"
_CREDENTIAL_STATUS = "rumi.resource.credential.status.v1"
_PROVIDER_MANAGE = "rumi.action.ai.provider.registry.manage.v1"
_PROVIDER_RESOURCE = "rumi.resource.ai.provider.registry.v1"

# Some mixed-protocol providers expose discovery from a gateway root while
# their generic OpenAI-compatible adapter expects a versioned API base.
_BUILTIN_PROVIDER_ADAPTER_ENDPOINTS = {
    "opencode-zen": "https://opencode.ai/zen/v1",
}


def run(input_data, context):
    """Preserve the finite legacy route without owning either resource."""
    data = dict(input_data or {})
    method = str(data.get("_method") or "GET").upper()
    if method == "GET":
        return ok(_status())
    if method != "POST":
        return error("unsupported method", "METHOD_NOT_ALLOWED")
    action = str(data.get("action") or "upsert").strip().lower()
    provider_id = str(data.get("provider_id") or "").strip()
    operation = f"ai.provider_key.{action}"
    approval_data = _approval_data(data)
    invalid = approval_invalid_response(operation, approval_data, error)
    if invalid is not None:
        return invalid
    approved = is_server_approved(
        context,
        operation=operation,
        input_data=approval_data,
    )
    if not approved:
        return ok(
            approval_required(
                operation,
                "high",
                args=approval_data,
                provider_id=provider_id,
            )
        )
    if not provider_id:
        return error("provider_id is required", "MISSING_PARAM")
    try:
        if action in {"delete", "delete_provider"}:
            return ok(_delete(provider_id))
        if action == "rename":
            return ok(_rename(provider_id, data))
        if action == "register_provider":
            return ok(_save_connection(provider_id, data, credential_handle=None))
        if action != "upsert":
            return error("unsupported action", "INVALID_ACTION")
        return ok(_upsert(provider_id, data))
    except (KeyError, RuntimeError, ValueError) as exc:
        return error(type(exc).__name__, "PROVIDER_CONNECTION_FAILED")


def _status() -> dict[str, Any]:
    credentials = _invoke(_CREDENTIAL_STATUS, "list", {})
    providers = _invoke(_PROVIDER_RESOURCE, "list", {})
    credential_items = (
        credentials.get("credentials") if isinstance(credentials, Mapping) else []
    )
    provider_items = (
        providers.get("providers") if isinstance(providers, Mapping) else []
    )
    provider_items = provider_items if isinstance(provider_items, list) else []
    return {
        "providers": [
            {
                "provider_id": str(
                    item.get("provider_instance_id") or ""
                ).removeprefix("provider."),
                "configured": bool(item.get("enabled", True)),
                "credential_handle": item.get("credential_handle"),
                "base_url": item.get("endpoint"),
                "kind": item.get("adapter_id"),
            }
            for item in provider_items
            if isinstance(item, Mapping)
        ],
        "credentials": (
            [dict(item) for item in credential_items if isinstance(item, Mapping)]
            if isinstance(credential_items, list) else []
        ),
        "custom_providers": [],
    }


def _upsert(provider_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
    secret = str(data.get("value") or "")
    if not secret:
        raise ValueError("provider credential value is required")
    provider_instance_id = f"provider.{provider_id}"
    created = _invoke(
        _CREDENTIAL_MANAGE,
        "create",
        {
            "secret_material": {"api_key": secret},
            "consumer_pack_id": "rumi_provider_adapters_pack",
            "provider_instance_id": provider_instance_id,
            "scopes": [
                "ai.generate",
                "ai.stream",
                "ai.embedding",
                "ai.image",
            ],
            "label": str(data.get("name") or provider_id),
        },
    )
    handle = str(created.get("handle") or "")
    try:
        _sync_legacy_provider_key(provider_id, secret, data)
        result = _save_connection(provider_id, data, credential_handle=handle)
        api_id = str(data.get("api_id") or "default").strip() or "default"
        model_availability = ModelAvailabilityService().after_provider_key_saved(
            provider_id,
            api_id,
            default_model=str(data.get("default_model") or ""),
            allowed_models=data.get("allowed_models"),
        )
    except Exception:
        _clear_legacy_provider_key(
            provider_id,
            api_id=str(data.get("api_id") or "default"),
        )
        _invoke(_CREDENTIAL_MANAGE, "revoke", {"handle": handle})
        raise
    return {
        **result,
        "api_id": api_id,
        "configured": True,
        "model_availability": model_availability,
    }


def _sync_legacy_provider_key(
    provider_id: str,
    secret: str,
    data: Mapping[str, Any],
) -> None:
    """Persist the approved connection for legacy chat execution.

    The provider registry remains the owner of connection metadata and its
    opaque broker handle. Until the legacy chat client is retired, it needs a
    synchronized encrypted compatibility copy to execute the same connection.
    """
    result = set_provider_api_key(
        provider_id,
        secret,
        api_id=str(data.get("api_id") or "default"),
        name=str(data.get("name") or data.get("api_id") or "default"),
        base_url=str(data.get("base_url") or data.get("endpoint") or ""),
        allowed_models=data.get("allowed_models"),
        default_model=str(data.get("default_model") or ""),
        notes=str(data.get("notes") or ""),
        quota_label=str(data.get("quota_label") or ""),
        kind=str(data.get("kind") or "openai-compatible"),
    )
    if not result.get("success"):
        raise RuntimeError("legacy provider key synchronization failed")


def _clear_legacy_provider_key(provider_id: str, *, api_id: str = "") -> None:
    """Remove the compatibility copy when connection creation rolls back."""
    try:
        set_provider_api_key(
            provider_id,
            "",
            api_id=api_id or None,
        )
    except (KeyError, RuntimeError, ValueError):
        # The broker rollback is authoritative; do not mask the original error.
        pass


def _save_connection(
    provider_id: str,
    data: Mapping[str, Any],
    *,
    credential_handle: str | None,
) -> dict[str, Any]:
    snapshot = _invoke(_PROVIDER_RESOURCE, "list", {})
    revision = int(snapshot.get("revision") or 0)
    endpoint = str(data.get("base_url") or data.get("endpoint") or "").strip()
    if not endpoint:
        endpoint = _builtin_provider_endpoint(provider_id)
    if not endpoint:
        raise ValueError("provider endpoint is required")
    adapter_id = _provider_adapter_id(provider_id)
    result = _invoke(
        _PROVIDER_MANAGE,
        "save",
        {
            "expected_revision": revision,
            "record": {
                "provider_instance_id": f"provider.{provider_id}",
                "adapter_id": adapter_id,
                "display_name": str(
                    data.get("label") or data.get("name") or provider_id
                ),
                "credential_handle": credential_handle,
                "endpoint": endpoint,
                "enabled": True,
                "metadata": {"legacy_api_id": str(data.get("api_id") or "default")},
            },
        },
    )
    return {
        "success": True,
        "provider_id": provider_id,
        "configured": True,
        "provider": result.get("provider"),
    }


def _provider_adapter_id(provider_id: str) -> str:
    """Map a provider connection to its executable protocol adapter."""
    if provider_id == "anthropic":
        return "anthropic"
    if provider_id == "openai":
        return "openai"
    return "openai-compatible"


def _builtin_provider_endpoint(provider_id: str) -> str:
    """Return a declared endpoint for a bundled provider, if one exists."""
    adapter_endpoint = _BUILTIN_PROVIDER_ADAPTER_ENDPOINTS.get(provider_id)
    if adapter_endpoint:
        return adapter_endpoint
    try:
        from domain.ai_client.providers import get_provider_catalog_map

        descriptor = get_provider_catalog_map().get(provider_id)
        if isinstance(descriptor, Mapping):
            metadata = descriptor.get("metadata")
            return str(
                descriptor.get("default_base_url")
                or (metadata.get("default_base_url") if isinstance(metadata, Mapping) else "")
                or ""
            ).strip()
    except Exception:
        return ""
    return ""


def _delete(provider_id: str) -> dict[str, Any]:
    snapshot = _invoke(_PROVIDER_RESOURCE, "list", {})
    providers = snapshot.get("providers") if isinstance(snapshot, Mapping) else []
    providers = providers if isinstance(providers, list) else []
    expected_id = f"provider.{provider_id}"
    record = next(
        (
            dict(item)
            for item in providers
            if isinstance(item, Mapping)
            and item.get("provider_instance_id") == expected_id
        ),
        None,
    )
    if record is None:
        raise KeyError("provider connection is unknown")
    _invoke(
        _PROVIDER_MANAGE,
        "delete",
        {
            "provider_instance_id": expected_id,
            "expected_revision": int(snapshot.get("revision") or 0),
        },
    )
    handle = record.get("credential_handle")
    if handle:
        _invoke(_CREDENTIAL_MANAGE, "revoke", {"handle": handle})
    metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
    _clear_legacy_provider_key(
        provider_id,
        api_id=str(metadata.get("legacy_api_id") or ""),
    )
    return {"success": True, "provider_id": provider_id, "configured": False}


def _rename(provider_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = _invoke(_PROVIDER_RESOURCE, "list", {})
    providers = snapshot.get("providers") if isinstance(snapshot, Mapping) else []
    providers = providers if isinstance(providers, list) else []
    expected_id = f"provider.{provider_id}"
    record = next(
        (
            dict(item)
            for item in providers
            if isinstance(item, Mapping)
            and item.get("provider_instance_id") == expected_id
        ),
        None,
    )
    if record is None:
        raise KeyError("provider connection is unknown")
    record["display_name"] = str(data.get("name") or provider_id)
    result = _invoke(
        _PROVIDER_MANAGE,
        "save",
        {"record": record, "expected_revision": int(snapshot.get("revision") or 0)},
    )
    return {
        "success": True,
        "provider_id": provider_id,
        "provider": result.get("provider"),
    }


def _approval_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Bind approval to a secret digest without persisting secret material."""
    result = dict(data)
    secret = str(result.pop("value", ""))
    if secret:
        result["value_sha256"] = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return result


def _invoke(contract_id: str, operation: str, payload: Mapping[str, Any]) -> Any:
    registry = get_container().get_or_none("v4_dispatch_session")
    if registry is None:
        raise RuntimeError("interface registry is unavailable")
    return invoke_global_contract(registry, contract_id, operation, dict(payload))
