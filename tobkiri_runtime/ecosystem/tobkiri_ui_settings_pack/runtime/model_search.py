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

# Only these client-supplied filter fields may reach the Host port.  The
# captured Profile identity is injected by the canonical presentation layer;
# no payload field may supply authority, credential, or owner material.
_FILTER_KEYS = frozenset(
    {
        "query",
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
    }
)

_RUNTIME_SETTING_KEYS = frozenset(
    {
        "api_bound_profiles",
        "composite_models",
        "model_packs",
        "model_notes",
    }
)


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
            client = invocation.contract_client(
                allowed_contract_ids=frozenset({MODEL_PROFILE_CONTRACT}),
                consumer_pack_id=PACK_ID,
                include_credentials=False,
            )
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
                    filters={
                        key: payload[key]
                        for key in keys
                        if key != "profile_id"
                    },
                    profiles=tuple(profiles),
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
