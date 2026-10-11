"""Captured, profile-bound public model access and catalog resources."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from tobkiri_host.broker import RequestEnvelope
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry
from ecosystem.rumi_provider_registry_pack.runtime.provider_filters import (
    CAPABILITY_REVISION,
    is_official_openrouter,
    normalize_native_filters,
)
from ecosystem.rumi_provider_registry_pack.runtime.model_access_presentation import (
    project_model_access,
    project_model_access_catalog,
)

PACK_ID = "rumi_provider_registry_pack"
READ_FUNCTION = "rumi_provider_registry_pack.model-access.read"
CATALOG_FUNCTION = "rumi_provider_registry_pack.model-access.catalog"
CONTRACT_ID = "tobkiri.resource.ai.provider.registry.v1"
READ_OPERATION = "rumi_provider_registry_pack.model-access-read"
CATALOG_OPERATION = "rumi_provider_registry_pack.model-access-catalog"
MODEL_CATALOG_CONTRACT = "tobkiri.resource.ai.model.catalog.v1"
MODEL_CATALOG_OPERATION = "rumi_model_catalog_pack.bundled-model-catalog"
MODEL_CATALOG_PROVIDER = "model-catalog.bundled"


def _connection(snapshot: Mapping[str, Any], identity: str) -> Mapping[str, Any]:
    providers = snapshot.get("providers")
    if not isinstance(providers, list):
        raise PermissionError("provider registry snapshot is invalid")
    matches = [
        item
        for item in providers
        if isinstance(item, Mapping) and item.get("provider_instance_id") == identity
    ]
    if len(matches) != 1 or matches[0].get("enabled") is not True:
        raise PermissionError("provider connection is unavailable")
    return matches[0]


def _catalog_identity(connection: Mapping[str, Any]) -> str:
    metadata = connection.get("metadata")
    provider = metadata.get("catalog_provider_id") if isinstance(metadata, Mapping) else None
    if not isinstance(provider, str) or not provider:
        raise PermissionError("provider catalog is unsupported")
    expected = (
        "anthropic"
        if provider == "anthropic"
        else (
            "local-openai-compatible"
            if provider in {"ollama", "lmstudio", "vllm", "llamacpp"}
            else "openai-compatible"
        )
    )
    if connection.get("adapter_id") != expected:
        raise PermissionError("provider catalog adapter is incompatible")
    return provider


class ModelAccessResourceHostFactoryV4:
    """Capture only the exact model access resource operations."""

    contract_id = CONTRACT_ID

    def __init__(self, operation: str = READ_OPERATION) -> None:
        """Select exactly one operation and its independently captured Function."""
        if operation not in {READ_OPERATION, CATALOG_OPERATION}:
            raise ValueError("model access operation is invalid")
        self.operation = operation
        self.function_id = READ_FUNCTION if operation == READ_OPERATION else CATALOG_FUNCTION
        self.operations = frozenset({operation})

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Bind current registry ownership and request envelope identities."""
        if (
            context.user_data_root is None
            or not context.profile_id
            or len(context.provider_bindings) != 1
            or any(
                binding.function.function_id != self.function_id
                or binding.operation.contract_id != CONTRACT_ID
                or binding.operation.operation_id not in self.operations
                for binding in context.provider_bindings
            )
        ):
            raise PermissionError("model access capture is invalid")
        contributions = []
        for binding in context.provider_bindings:
            key = (CONTRACT_ID, binding.operation.operation_id, binding.principal_ref.value)
            domain = context.domain_ids.get(key)
            if not domain:
                raise PermissionError("model access domain is unavailable")

            def invoke(
                operation_id: str,
                payload: Mapping[str, Any],
                invocation: HostProviderInvocationContextV4,
                selected: Any = binding,
                selected_domain: str = domain,
            ) -> Mapping[str, Any]:
                envelope = invocation.envelope
                if (
                    operation_id != selected.operation.operation_id
                    or not isinstance(envelope, RequestEnvelope)
                    or envelope.contract_id != CONTRACT_ID
                    or envelope.operation_id != operation_id
                    or envelope.target_principal.value != selected.principal_ref.value
                    or envelope.target_domain.value != selected_domain
                    or envelope.context.profile_id != context.profile_id
                    or envelope.context.plan_digest != context.plan_digest
                    or envelope.context.security_epoch != context.security_epoch
                ):
                    raise PermissionError("model access request capture changed")
                required = {"profile_id", "provider_instance_id"}
                allowed = required | (
                    {"discovery_filters"} if operation_id == CATALOG_OPERATION else set()
                )
                if (
                    not isinstance(payload, Mapping)
                    or not required <= set(payload)
                    or set(payload) - allowed
                    or payload.get("profile_id") != context.profile_id
                    or not isinstance(payload.get("provider_instance_id"), str)
                    or not payload["provider_instance_id"].strip()
                ):
                    raise PermissionError("model access payload is invalid")
                invocation.assert_current()
                registry = ProviderRegistry(
                    context.profile_id, user_data_root=context.user_data_root
                )
                snapshot = registry.snapshot()
                identity = payload["provider_instance_id"]
                connection = _connection(snapshot, identity)
                official = is_official_openrouter(connection)
                if operation_id == READ_OPERATION:
                    invocation.assert_current()
                    return project_model_access(
                        snapshot, identity, native_capability_confirmed=official
                    )
                provider = _catalog_identity(connection)
                catalog_payload: dict[str, Any] = {"provider_id": provider}
                if "discovery_filters" in payload:
                    normalized = normalize_native_filters(
                        {
                            "revision": CAPABILITY_REVISION,
                            "discovery": payload["discovery_filters"],
                            "routing": {},
                        },
                        verified_openrouter=official,
                    )
                    catalog_payload["discovery_filters"] = normalized["discovery"]
                client = invocation.contract_client(
                    allowed_contract_ids=frozenset({MODEL_CATALOG_CONTRACT}),
                    consumer_pack_id=PACK_ID,
                    include_credentials=False,
                )
                invocation.assert_current()
                catalog = client.invoke(
                    MODEL_CATALOG_CONTRACT,
                    MODEL_CATALOG_OPERATION,
                    catalog_payload,
                    provider_instance_id=MODEL_CATALOG_PROVIDER,
                )
                invocation.assert_current()
                if (
                    not isinstance(catalog, Mapping)
                    or catalog.get("success") is False
                    or catalog.get("ok") is False
                    or catalog.get("is_error") is True
                    or catalog.get("status") in {"failed", "error", "denied"}
                    or not isinstance(catalog.get("models"), list)
                ):
                    raise ValueError("model catalog result is invalid")
                # Re-read after discovery to reject deletion or policy changes during dispatch.
                current = registry.snapshot()
                if current != snapshot:
                    raise PermissionError("provider registry changed during catalog discovery")
                result = project_model_access_catalog(snapshot, identity, catalog)
                if provider != "openrouter":
                    supported = any(
                        isinstance(item, Mapping) and item.get("provider_id") == provider
                        for item in catalog.get("providers", [])
                    )
                    result["status"] = "live" if supported else "unavailable"
                return result

            contributions.append(
                HostProviderContributionV4(
                    contract_id=CONTRACT_ID,
                    contract_version=binding.operation.contract_version,
                    operation_id=binding.operation.operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain,
                    invoke=invoke,
                )
            )
        return CapturedHostProviderV4(tuple(contributions), lambda: None)


HOST_PROVIDER_FACTORY = {
    READ_FUNCTION: ModelAccessResourceHostFactoryV4(READ_OPERATION),
    CATALOG_FUNCTION: ModelAccessResourceHostFactoryV4(CATALOG_OPERATION),
}
