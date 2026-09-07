"""Read-only, Profile-captured settings presentation for the full Defaults UI."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from ecosystem.defaultspack.domain.frontend_settings_catalog import (
    SettingsCatalogInputs,
    SettingsSections,
)
from ecosystem.defaultspack.domain.frontend_settings_store import FrontendSettingsStore
from ecosystem.defaultspack.domain.frontend_builtin_catalog import builtin_ui_catalog

PACK_ID = "tobkiri_ui_settings_pack"
FUNCTION_ID = "tobkiri.ui.settings.read"
CONTRACT_ID = "tobkiri.resource.ui.settings.v1"
OPERATION_ID = "tobkiri_ui_settings_pack.settings-read"
CATALOG_OPERATION_ID = "tobkiri_ui_settings_pack.catalog-read"
MODEL_CONTRACT = "tobkiri.resource.ai.model.profile.v1"
MODEL_OPERATION = "rumi_model_registry_pack.model-profile-resource"


def _model_options(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    profiles = result.get("profiles")
    if not isinstance(profiles, list):
        raise ValueError("model profile list is unavailable")
    options = []
    for profile in profiles:
        if not isinstance(profile, dict) or not isinstance(profile.get("enabled"), bool):
            raise ValueError("invalid model profile")
        if not profile["enabled"]:
            continue
        identifier = profile.get("model_profile_id")
        label = profile.get("display_name")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("invalid model identity")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("invalid model label")
        options.append({"value": identifier, "label": label})
    return options


def _values(
    sections: list[dict[str, Any]], snapshot: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    # Only settings represented by known controls are exposed. Internal
    # receipts, arbitrary saved namespaces and credential controls stay local.
    values: dict[str, dict[str, Any]] = {}
    for section in sections:
        identifier = section["id"]
        saved = snapshot.get(identifier, {})
        if not isinstance(saved, dict):
            raise ValueError("invalid saved settings section")
        current: dict[str, Any] = {}
        for field in section["fields"]:
            name = field["id"]
            if field.get("type") in {"password", "api_keys", "external_tokens"}:
                continue
            if name in saved:
                current[name] = deepcopy(saved[name])
            elif "default" in field:
                current[name] = deepcopy(field["default"])
        values[identifier] = current
    return values


class SettingsReadHostFactoryV4:
    """Bind UI preferences and model reads to a reviewed Host capture."""

    function_id = FUNCTION_ID

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Prepare one exact read operation without reading or creating state."""
        if not context.profile_id or context.user_data_root is None:
            raise PermissionError("settings capture is incomplete")
        if not 1 <= len(context.provider_bindings) <= 2:
            raise PermissionError("settings binding is ambiguous")
        operation_ids: set[str] = set()
        for binding in context.provider_bindings:
            operation = binding.operation
            if (
                binding.function.function_id != FUNCTION_ID
                or operation.contract_id != CONTRACT_ID
                or operation.operation_id not in {OPERATION_ID, CATALOG_OPERATION_ID}
                or operation.operation_id in operation_ids
            ):
                raise PermissionError("settings binding is invalid")
            operation_ids.add(operation.operation_id)
            key = (CONTRACT_ID, operation.operation_id, binding.principal_ref.value)
            if context.domain_ids.get(key) is None:
                raise PermissionError("settings domain is unavailable")
        # This is the existing app-owned preferences location, never a path
        # from a request or the environment. No migration or repair is done.
        store = FrontendSettingsStore(
            context.user_data_root / "defaultspack" / "shared" / "frontend_settings.json"
        )

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if (
                operation_id not in operation_ids
                or payload.get("profile_id") != context.profile_id
                or set(payload) - {"profile_id", "_session_id"}
            ):
                raise PermissionError("settings read request is invalid")
            client = invocation.contract_client(
                allowed_contract_ids=frozenset({MODEL_CONTRACT}),
                consumer_pack_id=PACK_ID,
            )
            models = client.invoke(
                MODEL_CONTRACT,
                MODEL_OPERATION,
                {"profile_id": context.profile_id, "operation": "list"},
            )
            inputs = SettingsCatalogInputs(
                input_templates=[],
                output_templates=[],
                input_profile_options=[],
                output_profile_options=[],
                model_options=_model_options(models),
                model_route_options=_model_options(models),
                api_key_status=[],
            )
            sections = SettingsSections().build([], [], template_catalog={}, inputs=inputs)
            settings = {"sections": sections, "values": _values(sections, store.read_snapshot())}
            if operation_id == OPERATION_ID:
                return settings
            definitions = builtin_ui_catalog()
            return {
                "app": {"id": "defaultspack", "name": "Tobkiri"},
                "shell": definitions["shell"],
                "parts": definitions["parts"],
                "component_bindings": definitions["component_bindings"],
                "sidebar": {
                    "filters": definitions["sidebar_filters"],
                    "items": [
                        *definitions["sidebar_primary_items"],
                        *definitions["sidebar_system_items"],
                    ],
                },
                "settings": settings,
                "chat_rendering": {"renderers": definitions["chat_renderers"]},
                "extension_points": definitions["extension_points"],
            }

        return CapturedHostProviderV4(
            tuple(
                HostProviderContributionV4(
                    contract_id=CONTRACT_ID,
                    contract_version=binding.operation.contract_version,
                    operation_id=binding.operation.operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=context.domain_ids[
                        (CONTRACT_ID, binding.operation.operation_id, binding.principal_ref.value)
                    ],
                    invoke=invoke,
                )
                for binding in context.provider_bindings
            ),
            lambda: None,
        )


HOST_PROVIDER_FACTORY = {FUNCTION_ID: SettingsReadHostFactoryV4()}
