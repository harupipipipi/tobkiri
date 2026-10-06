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

from .local_endpoint import local_openai_endpoint
from .registry import ProviderRegistry, ProviderRegistryConflict

CREDENTIAL_CONTRACT = "tobkiri.action.credential.manage.v1"
CREDENTIAL_OPERATION = "rumi_credential_broker_pack.credential-manage"
PREPARE_OPERATION = "rumi_provider_registry_pack.provider-configure-prepare"
EXECUTE_OPERATION = "rumi_provider_registry_pack.provider-configure"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_FIELDS = {"connection_name", "protocol", "endpoint", "key_value"}
_OPTIONAL_FIELDS = {"catalog_provider_id", "display_name"}


def configuration_request(payload: Mapping[str, Any]) -> dict[str, str]:
    """Validate setup data without accepting Profile or authority selections."""
    if (
        not _FIELDS.issubset(payload)
        or set(payload) - (_FIELDS | _OPTIONAL_FIELDS)
        or any(type(value) is not str for value in payload.values())
    ):
        raise ValueError("provider configuration fields are invalid")
    name = payload["connection_name"]
    key = payload["key_value"]
    endpoint = payload["endpoint"]
    protocol = payload["protocol"]
    catalog_provider_id = payload.get("catalog_provider_id")
    display_name = payload.get("display_name")
    if (
        (catalog_provider_id is not None
         and _NAME.fullmatch(catalog_provider_id) is None)
        or (display_name is not None and (
            not display_name.strip() or len(display_name) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in display_name)
        ))
    ):
        raise ValueError("provider catalog identity or display name is invalid")
    if (
        _NAME.fullmatch(name) is None
        or protocol not in {
            "openai-compatible", "anthropic", "local-openai-compatible",
        }
        or len(key) > 16_384
        or any(ord(char) < 32 or ord(char) == 127 for char in key)
        or len(endpoint) > 2_048
    ):
        raise ValueError("provider configuration values are invalid")
    if protocol == "local-openai-compatible":
        if key:
            raise ValueError("local provider credentials are not permitted")
        normalized = dict(payload)
        normalized["endpoint"] = local_openai_endpoint(endpoint)
        return normalized
    if not key:
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
        or (display_name is not None and key in display_name)
        or (catalog_provider_id is not None and key in catalog_provider_id)
    ):
        raise ValueError("provider endpoint is invalid")
    return dict(payload)


def prepare_configuration(
    registry: ProviderRegistry, payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Read the existing revision and describe a write without storing a key."""
    request = configuration_request(payload)
    plan = {
        "profile_id": registry.profile_id,
        "provider_instance_id": "provider." + request["connection_name"],
        "adapter_id": request["protocol"],
        "endpoint": request["endpoint"],
        "expected_revision": registry.snapshot()["revision"],
        # The coordinator later verifies the exact submitted request. Keep its
        # digest raw while freezing the separately canonicalized endpoint.
        "request_digest": canonical_digest(dict(payload)),
    }
    registry.prepare_save(
        _connection_record(request, plan, None),
        expected_revision=plan["expected_revision"],
    )
    return plan


def _connection_record(
    request: Mapping[str, Any],
    plan: Mapping[str, Any],
    credential_handle: str | None,
) -> dict[str, Any]:
    """Build the exact public provider record covered by the prepared request."""
    record: dict[str, Any] = {
        "provider_instance_id": plan["provider_instance_id"],
        "adapter_id": plan["adapter_id"],
        "endpoint": plan["endpoint"],
        "credential_handle": credential_handle,
    }
    if request.get("display_name"):
        record["display_name"] = request["display_name"]
    if request.get("catalog_provider_id"):
        # Discovery metadata does not attest account access or capabilities.
        # It is covered by the frozen request digest and never grants authority.
        record["metadata"] = {
            "catalog_provider_id": request["catalog_provider_id"],
        }
    return record


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
    prepared = registry.prepare_save(
        _connection_record(request, plan, None),
        expected_revision=plan["expected_revision"],
    )
    if request["protocol"] == "local-openai-compatible":
        try:
            registry.save(prepared, expected_revision=plan["expected_revision"])
        except Exception as exc:
            if isinstance(exc, (ValueError, ProviderRegistryConflict)) or not (
                _connection_save_confirmed(
                    _save_snapshot(registry), prepared,
                    expected_revision=plan["expected_revision"],
                )
            ):
                raise RuntimeError(
                    "provider connection save was not confirmed"
                ) from None
        return {
            "configured": True,
            "provider_instance_id": prepared["provider_instance_id"],
        }
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
    record = {**prepared, "credential_handle": handle}
    try:
        registry.save(record, expected_revision=plan["expected_revision"])
    except Exception as exc:
        # Confirm the complete intended record and both owner revisions after
        # an uncertain ACK. Known policy/CAS rejections cannot confirm a save.
        snapshot = _save_snapshot(registry)
        if isinstance(exc, (ValueError, ProviderRegistryConflict)) or not (
            _connection_save_confirmed(
                snapshot, record, expected_revision=plan["expected_revision"],
            )
        ):
            if any(
                item.get("credential_handle") == handle
                for item in snapshot["providers"]
            ):
                raise RuntimeError("provider connection save was not confirmed") from None
            try:
                client.invoke(CREDENTIAL_CONTRACT, CREDENTIAL_OPERATION, {
                    "operation": "revoke", "profile_id": registry.profile_id,
                    "handle": handle,
                })
            except Exception:
                raise RuntimeError(
                    "provider configuration cleanup was not confirmed"
                ) from None
            raise RuntimeError("provider connection save was not confirmed") from None
    return {
        "configured": True,
        "provider_instance_id": record["provider_instance_id"],
    }


def _save_snapshot(registry: ProviderRegistry) -> dict[str, Any]:
    """Read acknowledgement evidence without revoking an uncertain live handle."""
    try:
        return registry.snapshot()
    except Exception:
        raise RuntimeError("provider connection save was not confirmed") from None


def _connection_save_confirmed(
    snapshot: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    expected_revision: int,
) -> bool:
    """Require the entire intended record and its exact committed revisions."""
    if snapshot["revision"] != expected_revision + 1:
        return False
    matches = [
        item for item in snapshot["providers"]
        if item["provider_instance_id"] == record["provider_instance_id"]
    ]
    if len(matches) != 1:
        return False
    saved = matches[0]
    if not all(isinstance(saved.get(key), str) for key in ("created_at", "updated_at")):
        return False
    return {
        key: value for key, value in saved.items()
        if key not in {"created_at", "updated_at"}
    } == dict(record)
