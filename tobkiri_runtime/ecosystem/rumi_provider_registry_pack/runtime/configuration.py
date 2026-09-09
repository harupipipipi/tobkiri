"""Provider setup using existing credential and connection owners.

The approval coordinator freezes the execute request. This module owns neither
approval state nor retry state, and never retries an uncertain credential write.
"""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from core_runtime.global_contract_dispatch import GlobalContractClient
from tobkiri_protocol.canonical import canonical_digest

from .registry import ProviderRegistry

CREDENTIAL_CONTRACT = "tobkiri.action.credential.manage.v1"
CREDENTIAL_OPERATION = "rumi_credential_broker_pack.credential-manage"
PREPARE_OPERATION = "rumi_provider_registry_pack.provider-configure-prepare"
EXECUTE_OPERATION = "rumi_provider_registry_pack.provider-configure"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_FIELDS = {"connection_name", "protocol", "endpoint", "key_value"}


def configuration_request(payload: Mapping[str, Any]) -> dict[str, str]:
    """Validate setup data without accepting Profile or authority selections."""
    if set(payload) != _FIELDS or any(type(value) is not str for value in payload.values()):
        raise ValueError("provider configuration fields are invalid")
    name = payload["connection_name"]
    key = payload["key_value"]
    endpoint = payload["endpoint"]
    if (
        _NAME.fullmatch(name) is None
        or payload["protocol"] not in {"openai-compatible", "anthropic"}
        or not key or len(key) > 16_384
        or any(ord(char) < 32 or ord(char) == 127 for char in key)
        or len(endpoint) > 2_048
    ):
        raise ValueError("provider configuration values are invalid")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        raise ValueError("provider endpoint is invalid") from None
    if (
        parsed.scheme != "https" or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
        or any(char.isspace() for char in endpoint)
        or key in endpoint or key in name
    ):
        raise ValueError("provider endpoint is invalid")
    return dict(payload)


def prepare_configuration(
    registry: ProviderRegistry, payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Read the existing revision and describe a write without storing a key."""
    request = configuration_request(payload)
    return {
        "profile_id": registry.profile_id,
        "provider_instance_id": "provider." + request["connection_name"],
        "adapter_id": request["protocol"],
        "endpoint": request["endpoint"],
        "expected_revision": registry.snapshot()["revision"],
        "request_digest": canonical_digest(request),
    }


def execute_configuration(
    registry: ProviderRegistry,
    client: GlobalContractClient,
    payload: Mapping[str, Any],
    *,
    consumer_pack_id: str,
) -> dict[str, Any]:
    """Execute one frozen setup after Broker approval, with no write retry."""
    if set(payload) != {"request", "plan"}:
        raise ValueError("provider configuration execution is invalid")
    request = payload["request"]
    plan = payload["plan"]
    if not isinstance(request, Mapping) or not isinstance(plan, Mapping):
        raise ValueError("provider configuration execution is invalid")
    # Re-read before creating a credential. A stale approval cannot overwrite a
    # connection changed since prepare, nor leave a key behind for that conflict.
    if dict(plan) != prepare_configuration(registry, request):
        raise PermissionError("provider configuration changed after preparation")
    try:
        created = client.invoke(CREDENTIAL_CONTRACT, CREDENTIAL_OPERATION, {
            "operation": "create", "profile_id": registry.profile_id,
            "secret_material": {"api_key": request["key_value"]},
            "consumer_pack_id": consumer_pack_id,
            "provider_instance_id": plan["provider_instance_id"],
            "scopes": ["ai.generate", "ai.stream"],
        })
    except Exception:
        # A missing ACK does not authorize another create. PendingEffect records
        # the failed/ambiguous attempt and remains the retry/status authority.
        raise RuntimeError("provider credential save was not confirmed") from None
    handle = created.get("handle") if isinstance(created, Mapping) else None
    if (
        not isinstance(handle, str) or not handle.startswith("credential:")
        or created.get("profile_id") != registry.profile_id
        or created.get("provider_instance_id") != plan["provider_instance_id"]
        or created.get("consumer_pack_id") != consumer_pack_id
    ):
        raise RuntimeError("provider credential save was not confirmed")
    record = {
        "provider_instance_id": plan["provider_instance_id"],
        "adapter_id": plan["adapter_id"], "endpoint": plan["endpoint"],
        "credential_handle": handle,
    }
    try:
        registry.save(record, expected_revision=plan["expected_revision"])
    except Exception:
        # The owner may have committed before its ACK was lost. Confirm that
        # exact handle before considering cleanup; never revoke a live binding.
        try:
            matches = [item for item in registry.snapshot()["providers"]
                       if item["provider_instance_id"] == record["provider_instance_id"]]
        except Exception:
            raise RuntimeError("provider connection save was not confirmed") from None
        if len(matches) != 1 or any(matches[0].get(key) != value for key, value in record.items()):
            try:
                client.invoke(CREDENTIAL_CONTRACT, CREDENTIAL_OPERATION, {
                    "operation": "revoke", "profile_id": registry.profile_id,
                    "handle": handle,
                })
            except Exception:
                raise RuntimeError("provider configuration cleanup was not confirmed") from None
            raise RuntimeError("provider connection save was not confirmed") from None
    return {
        "configured": True,
        "provider_instance_id": record["provider_instance_id"],
    }
