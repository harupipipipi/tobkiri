"""Captured, Profile-bound model catalog search for the Defaults UI."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from ecosystem.tobkiri_ui_settings_pack.runtime.store import FrontendSettingsStore
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.ports import ModelSearchCommand

PACK_ID = "tobkiri_ui_settings_pack"
FUNCTION_ID = "tobkiri.ui.model-search.read"
CONTRACT_ID = "tobkiri.resource.ui.model-search.v1"
OPERATION_ID = "tobkiri_ui_settings_pack.model-search"
MODEL_PROFILE_CONTRACT = "tobkiri.resource.ai.model.profile.v1"
MODEL_PROFILE_OPERATION = "rumi_model_registry_pack.model-profile-resource"
MODEL_CATALOG_CONTRACT = "tobkiri.resource.ai.model.catalog.v1"
MODEL_CATALOG_OPERATION = "rumi_model_catalog_pack.bundled-model-catalog"
MODEL_CATALOG_PROVIDER = "model-catalog.bundled"
PROVIDER_REGISTRY_CONTRACT = "tobkiri.resource.ai.provider.registry.v1"
PROVIDER_REGISTRY_OPERATION = "rumi_provider_registry_pack.provider-registry-resource"

# Only these client-supplied filter fields may reach the Host port.  The
# captured Profile identity is injected by the canonical presentation layer;
# no payload field may supply authority, credential, or owner material.
_FILTER_KEYS = frozenset(
    {
        "query",
        "connection_id",
        "type",
        "model_type",
        "requires",
        "speed_tier",
        "provider_id",
        "provider",
        "configured_only",
        "local_only",
        "min_knowledge_level",
        "max_results",
        "offset",
    }
)

_RUNTIME_SETTING_KEYS = frozenset(
    {
        "api_bound_profiles",
        "composite_models",
        "model_packs",
        "model_notes",
        "preferred_model",
    }
)


def connection_search_filters(
    filters: Mapping[str, Any], snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve public catalog identity from one exact enabled connection."""
    connection_id = filters.get("connection_id")
    if not isinstance(connection_id, str) or not connection_id.strip():
        raise PermissionError("model search connection is invalid")
    providers = snapshot.get("providers") if isinstance(snapshot, Mapping) else None
    if not isinstance(providers, list):
        raise PermissionError("model search connection is unavailable")
    matches = [item for item in providers if isinstance(item, Mapping)
               and item.get("provider_instance_id") == connection_id]
    if len(matches) != 1 or matches[0].get("enabled") is not True:
        raise PermissionError("model search connection is unavailable")
    selected = matches[0]
    provider = selected.get("catalog_provider_id")
    adapter = selected.get("adapter_id")
    if not isinstance(provider, str) or not provider:
        raise PermissionError("model search connection catalog is unavailable")
    # These are the protocols accepted by the approved connection setup path.
    expected = "anthropic" if provider == "anthropic" else (
        "local-openai-compatible"
        if provider in {"ollama", "lmstudio", "vllm", "llamacpp"}
        else "openai-compatible"
    )
    if adapter != expected:
        raise PermissionError("model search connection provider is incompatible")
    if any(filters.get(key) not in (None, "", provider)
           for key in ("provider", "provider_id")):
        raise PermissionError("model search connection provider does not match")
    return {**dict(filters), "provider_id": provider}


class ModelSearchHostFactoryV4:
    """Bind one verified search Function to the narrow Host search port."""

    function_id = FUNCTION_ID

    def capture(
        self, context: HostProviderCaptureContextV4
    ) -> CapturedHostProviderV4:
        """Capture the exact operation without reading settings or catalogs."""
        port = context.model_search_port
        bindings = tuple(context.provider_bindings)
        if (
            not context.profile_id
            or port is None
            or len(bindings) != 1
            or bindings[0].function.function_id != FUNCTION_ID
            or bindings[0].operation.contract_id != CONTRACT_ID
            or bindings[0].operation.operation_id != OPERATION_ID
        ):
            raise PermissionError("model search capture is invalid")
        binding = bindings[0]
        key = (
            binding.operation.contract_id,
            binding.operation.operation_id,
            binding.principal_ref.value,
        )
        domain_id = context.domain_ids.get(key)
        if domain_id is None:
            raise PermissionError("model search domain is unavailable")

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if operation_id != OPERATION_ID:
                raise PermissionError("model search operation is invalid")
            envelope = invocation.envelope
            if not isinstance(envelope, RequestEnvelope):
                raise PermissionError("model search envelope is invalid")
            if (
                envelope.contract_id != CONTRACT_ID
                or envelope.operation_id != operation_id
                or envelope.target_principal.value != binding.principal_ref.value
                or envelope.context.profile_id != context.profile_id
                or envelope.context.plan_digest != context.plan_digest
                or envelope.context.security_epoch != context.security_epoch
            ):
                raise PermissionError("model search capture changed")
            if not isinstance(payload, Mapping):
                raise PermissionError("model search payload is invalid")
            keys = set(payload) - {"_session_id"}
            if (
                payload.get("profile_id") != context.profile_id
                or not keys <= (_FILTER_KEYS | {"profile_id"})
            ):
                raise PermissionError("model search request is invalid")
            invocation.assert_current()
            allowed_contracts = {MODEL_PROFILE_CONTRACT, MODEL_CATALOG_CONTRACT}
            if "connection_id" in keys:
                allowed_contracts.add(PROVIDER_REGISTRY_CONTRACT)
            client = invocation.contract_client(
                allowed_contract_ids=frozenset(allowed_contracts),
                consumer_pack_id=PACK_ID,
                include_credentials=False,
            )
            filters = {key: payload[key] for key in keys if key != "profile_id"}
            if "connection_id" in keys:
                providers = client.invoke(
                    PROVIDER_REGISTRY_CONTRACT,
                    PROVIDER_REGISTRY_OPERATION,
                    {"profile_id": context.profile_id},
                )
                invocation.assert_current()
                filters = connection_search_filters(filters, providers)
            snapshot = client.invoke(
                MODEL_PROFILE_CONTRACT,
                MODEL_PROFILE_OPERATION,
                {"profile_id": context.profile_id, "operation": "list"},
            )
            invocation.assert_current()
            if not isinstance(snapshot, Mapping):
                raise PermissionError("model profile snapshot is invalid")
            profiles = snapshot.get("profiles")
            if not isinstance(profiles, list) or any(
                not isinstance(profile, Mapping) for profile in profiles
            ):
                raise PermissionError("model profile snapshot is invalid")
            catalog = client.invoke(
                MODEL_CATALOG_CONTRACT,
                MODEL_CATALOG_OPERATION,
                {},
                provider_instance_id=MODEL_CATALOG_PROVIDER,
            )
            invocation.assert_current()
            if not isinstance(catalog, Mapping):
                raise PermissionError("model catalog snapshot is invalid")
            catalog_models = catalog.get("models")
            if not isinstance(catalog_models, list) or any(
                not isinstance(model, Mapping) for model in catalog_models
            ):
                raise PermissionError("model catalog snapshot is invalid")
            settings_path = (
                context.user_data_root
                / "defaultspack"
                / "shared"
                / "frontend_settings.json"
            )
            settings_snapshot = FrontendSettingsStore(settings_path).read_snapshot()
            raw_models = settings_snapshot.get("models", {})
            if not isinstance(raw_models, Mapping):
                raise PermissionError("model settings snapshot is invalid")
            runtime_settings = {
                key: deepcopy(raw_models[key])
                for key in _RUNTIME_SETTING_KEYS
                if key in raw_models
            }
            return port.search_models(
                ModelSearchCommand(
                    context=envelope.context,
                    profile_id=context.profile_id,
                    filters=filters,
                    profiles=tuple(profiles),
                    catalog_models=tuple(catalog_models),
                    runtime_settings=runtime_settings,
                )
            )

        contribution = HostProviderContributionV4(
            contract_id=binding.operation.contract_id,
            contract_version=binding.operation.contract_version,
            operation_id=binding.operation.operation_id,
            principal_id=binding.principal_ref.value,
            artifact_digest=binding.artifact.digest,
            implementation_digest=binding.function.implementation_digest,
            domain_id=domain_id,
            invoke=invoke,
        )
        return CapturedHostProviderV4((contribution,), lambda: None)


HOST_PROVIDER_FACTORY = {FUNCTION_ID: ModelSearchHostFactoryV4()}
