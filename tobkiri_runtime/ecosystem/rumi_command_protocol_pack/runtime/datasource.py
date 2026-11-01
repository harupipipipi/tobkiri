"""Query finite Command Protocol datasources through canonical registries."""

from __future__ import annotations

import unicodedata
from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from tobkiri_protocol.canonical import canonical_digest

FUNCTION_ID = "rumi_command_protocol_pack.command.datasource"
CONTRACT_ID = "tobkiri.resource.command.datasource.v1"
OPERATION_ID = "command.datasource.query"
MODEL_CONTRACT = "tobkiri.resource.ai.model.profile.v1"
MODEL_OPERATION = "rumi_model_registry_pack.model-profile-resource"
PROVIDER_CONTRACT = "tobkiri.resource.ai.provider.registry.v1"
PROVIDER_OPERATION = "rumi_provider_registry_pack.provider-registry-resource"
MODEL_DATASOURCE = "tobkiri:model_catalog"
PROVIDER_DATASOURCE = "tobkiri:provider_catalog"
_DATASOURCES = frozenset({MODEL_DATASOURCE, PROVIDER_DATASOURCE})
_ALLOWED_FIELDS = frozenset(
    {
        "profile_id", "datasource_ref", "query", "cursor", "limit",
        "selected_values", "request_id", "_session_id",
    }
)


def _owner_identity(value: object, field: str) -> str:
    text = value if isinstance(value, str) else ""
    if (
        not text
        or len(text) > 512
        or text.strip() != text
        or any(ord(character) < 0x20 for character in text)
    ):
        raise PermissionError(f"{field} is invalid")
    return text


def _search_text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _request(payload: Mapping[str, Any]) -> tuple[str, str, int, int, list[str]]:
    datasource_ref = payload.get("datasource_ref")
    if datasource_ref not in _DATASOURCES:
        raise PermissionError("command datasource is not owned by this provider")
    query = payload.get("query", "")
    cursor = payload.get("cursor", "0")
    limit = payload.get("limit", 25)
    selected = payload.get("selected_values", [])
    if (
        not isinstance(query, str)
        or len(query) > 200
        or not isinstance(cursor, str)
        or not cursor.isdecimal()
        or len(cursor) > 8
        or type(limit) is not int
        or not 1 <= limit <= 100
        or not isinstance(selected, list)
        or len(selected) > 100
        or any(not isinstance(item, str) or not item or len(item) > 256 for item in selected)
    ):
        raise ValueError("command datasource query is invalid")
    request_id = payload.get("request_id", "")
    if request_id and (
        not isinstance(request_id, str)
        or len(request_id) > 160
        or any(ord(character) < 0x20 for character in request_id)
    ):
        raise ValueError("command datasource request ID is invalid")
    return datasource_ref, _search_text(query), int(cursor), limit, selected


def _model_items(
    profiles: list[Any], providers: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    items = []
    for profile in profiles:
        if not isinstance(profile, Mapping):
            raise ValueError("model registry record is invalid")
        identity = profile.get("model_profile_id")
        label = profile.get("display_name")
        model_id = profile.get("model_id")
        enabled = profile.get("enabled")
        metadata = profile.get("metadata")
        provider_id = (
            metadata.get("provider_connection_id")
            if isinstance(metadata, Mapping)
            else None
        )
        if (
            not isinstance(identity, str)
            or not identity
            or not isinstance(label, str)
            or not label
            or not isinstance(model_id, str)
            or not isinstance(enabled, bool)
            or (provider_id is not None and not isinstance(provider_id, str))
        ):
            raise ValueError("model registry record is invalid")
        provider = providers.get(provider_id or "", {})
        selectable = enabled and provider.get("enabled") is True
        items.append(
            {
                "id": identity,
                "value": identity,
                "label": {"fallback": label},
                "description": {"fallback": f"{provider_id or 'unbound'} · {model_id}"},
                "icon": "model",
                "badges": ([{"label": "Configured", "tone": "success"}] if selectable else []),
                "disabled": not selectable,
                "disabled_reason": (
                    None if selectable else {"fallback": "Provider connection is unavailable"}
                ),
                "metadata": {
                    "provider_id": provider_id,
                    "configured": provider.get("enabled") is True,
                    "available": selectable,
                },
            }
        )
    return sorted(items, key=lambda item: _search_text(item["label"]["fallback"]))


def _provider_items(
    providers: list[Any], profiles: list[Any]
) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for profile in profiles:
        metadata = profile.get("metadata") if isinstance(profile, Mapping) else None
        provider_id = (
            metadata.get("provider_connection_id")
            if isinstance(metadata, Mapping)
            else None
        )
        if isinstance(provider_id, str):
            counts[provider_id] = counts.get(provider_id, 0) + 1
    items = []
    for provider in providers:
        if not isinstance(provider, Mapping):
            raise ValueError("provider registry record is invalid")
        identity = provider.get("provider_instance_id")
        label = provider.get("display_name")
        enabled = provider.get("enabled")
        if (
            not isinstance(identity, str)
            or not identity
            or not isinstance(label, str)
            or not label
            or not isinstance(enabled, bool)
        ):
            raise ValueError("provider registry record is invalid")
        count = counts.get(identity, 0)
        items.append(
            {
                "id": identity,
                "value": identity,
                "label": {"fallback": label},
                "description": {"fallback": f"{count} models"},
                "icon": "provider",
                "badges": ([{"label": "Configured", "tone": "success"}] if enabled else []),
                "disabled": not enabled,
                "disabled_reason": (
                    None if enabled else {"fallback": "Provider connection is disabled"}
                ),
                "metadata": {
                    "provider_id": identity,
                    "configured": enabled,
                    "available": enabled,
                    "model_count": count,
                },
            }
        )
    return sorted(items, key=lambda item: _search_text(item["label"]["fallback"]))


class CommandDatasourceHostFactoryV4:
    """Capture one Profile-bound datasource projection."""

    function_id = FUNCTION_ID

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Capture without reading either registry."""
        if not context.profile_id or len(context.provider_bindings) != 1:
            raise PermissionError("command datasource capture is incomplete")
        binding = context.provider_bindings[0]
        operation = binding.operation
        if (
            binding.function.function_id != FUNCTION_ID
            or operation.contract_id != CONTRACT_ID
            or operation.operation_id != OPERATION_ID
            or operation.contract_version != "1.0.0"
        ):
            raise PermissionError("command datasource binding is invalid")
        key = (CONTRACT_ID, OPERATION_ID, binding.principal_ref.value)
        domain_id = context.domain_ids.get(key)
        if domain_id is None:
            raise PermissionError("command datasource domain is unavailable")

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            invocation.assert_current()
            if (
                operation_id != OPERATION_ID
                or set(payload) - _ALLOWED_FIELDS
                or payload.get("profile_id") != context.profile_id
            ):
                raise PermissionError("command datasource request is invalid")
            owner_principal = _owner_identity(
                invocation.presentation_owner_principal_id,
                "presentation owner principal",
            )
            owner_session = _owner_identity(
                invocation.presentation_owner_session_id,
                "presentation owner session",
            )
            datasource_ref, query, offset, limit, selected = _request(payload)
            client = invocation.contract_client(
                allowed_contract_ids=frozenset({MODEL_CONTRACT, PROVIDER_CONTRACT}),
                consumer_pack_id="rumi_command_protocol_pack",
                include_credentials=False,
            )
            models = client.invoke(
                MODEL_CONTRACT,
                MODEL_OPERATION,
                {"profile_id": context.profile_id, "operation": "list"},
            )
            invocation.assert_current()
            providers = client.invoke(PROVIDER_CONTRACT, PROVIDER_OPERATION, {})
            invocation.assert_current()
            if not isinstance(models, Mapping) or not isinstance(providers, Mapping):
                raise ValueError("command datasource owners are unavailable")
            model_revision = models.get("revision")
            provider_revision = providers.get("revision")
            profiles = models.get("profiles")
            connections = providers.get("providers")
            if (
                type(model_revision) is not int
                or model_revision < 0
                or type(provider_revision) is not int
                or provider_revision < 0
                or not isinstance(profiles, list)
                or not isinstance(connections, list)
            ):
                raise ValueError("command datasource owner revision is invalid")
            provider_map = {
                item["provider_instance_id"]: item
                for item in connections
                if isinstance(item, Mapping)
                and isinstance(item.get("provider_instance_id"), str)
            }
            items = (
                _model_items(profiles, provider_map)
                if datasource_ref == MODEL_DATASOURCE
                else _provider_items(connections, profiles)
            )
            if query:
                items = [
                    item for item in items
                    if query in _search_text(
                        " ".join(
                            (
                                item["value"], item["label"]["fallback"],
                                item["description"]["fallback"],
                            )
                        )
                    )
                ]
            retained = [item for item in items if item["value"] in set(selected)]
            page = items[offset : offset + limit]
            if offset == 0 and retained:
                retained_ids = {item["value"] for item in retained}
                page = [*retained, *(item for item in page if item["value"] not in retained_ids)][:limit]
            next_offset = offset + len(page)
            request_id = payload.get("request_id") or canonical_digest(
                {
                    "profile_id": context.profile_id,
                    "owner_principal_id": owner_principal,
                    "owner_session_id": owner_session,
                    "datasource_ref": datasource_ref,
                    "query": query,
                    "cursor": offset,
                    "limit": limit,
                    "selected_values": selected,
                }
            )
            revision = canonical_digest(
                {"models": model_revision, "providers": provider_revision}
            )
            return {
                "api_version": "tobkiri.commands/v1",
                "status": "succeeded",
                "datasource_ref": datasource_ref,
                "request_id": request_id,
                "revision": revision,
                "items": page,
                "page": {
                    "has_more": next_offset < len(items),
                    "next_cursor": str(next_offset) if next_offset < len(items) else None,
                },
            }

        return CapturedHostProviderV4(
            (
                HostProviderContributionV4(
                    contract_id=CONTRACT_ID,
                    contract_version=operation.contract_version,
                    operation_id=OPERATION_ID,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain_id,
                    invoke=invoke,
                ),
            ),
            lambda: None,
        )


HOST_PROVIDER_FACTORY = {FUNCTION_ID: CommandDatasourceHostFactoryV4()}
