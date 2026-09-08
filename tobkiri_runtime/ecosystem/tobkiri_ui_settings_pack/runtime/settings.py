"""Read-only, Profile-captured settings presentation for the full Defaults UI."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from ecosystem.defaultspack.domain.frontend_settings_store import FrontendSettingsStore

PACK_ID = "tobkiri_ui_settings_pack"
FUNCTION_ID = "tobkiri.ui.settings.read"
CATALOG_FUNCTION_ID = "tobkiri.ui.catalog.read"
CONTRACT_ID = "tobkiri.resource.ui.settings.v1"
OPERATION_ID = "tobkiri_ui_settings_pack.settings-read"
CATALOG_OPERATION_ID = "tobkiri_ui_settings_pack.catalog-read"
MODEL_CONTRACT = "tobkiri.resource.ai.model.profile.v1"
MODEL_OPERATION = "rumi_model_registry_pack.model-profile-resource"
PRESENTATION_CONTRACT = "tobkiri.resource.application.presentation.v1"
PRESENTATION_OPERATION = "defaultspack.presentation.read"
WRITE_FUNCTION_ID = "tobkiri.ui.preferences.write"
WRITE_CONTRACT_ID = "tobkiri.action.ui.preferences.v1"
WRITE_OPERATION_ID = "tobkiri_ui_settings_pack.preferences-write"

# This write policy is intentionally separate from public-settings-fields.
# Model, tool, integration and credential mutations need their own owner policy.
_PREFERENCE_TYPES = {
    "general": {
        "composer_placeholder": str,
        "show_activity_in_messages": bool,
        "keyboard_button_navigation": bool,
        "spotlight_shortcut_enabled": bool,
        "spotlight_shortcut": str,
        "spotlight_shortcut_text_input": bool,
        "language": str,
    },
    "chat_rendering": {"show_widgets": bool, "unknown_block_strategy": str},
}


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
    # This is Host-owned disclosure policy, not supplied by the PackVM UI.
    # New public saved fields require an explicit reviewed policy change.
    public_fields = json.loads((Path(__file__).with_name("public-settings-fields.v1.json")).read_text(encoding="utf-8"))
    for section in sections:
        identifier = section["id"]
        saved = snapshot.get(identifier, {})
        if not isinstance(saved, dict):
            raise ValueError("invalid saved settings section")
        current: dict[str, Any] = {}
        for field in section["fields"]:
            name = field["id"]
            if name not in public_fields.get(identifier, []) or field.get("type") in {"password", "api_keys", "external_tokens"}:
                continue
            if name in saved:
                current[name] = deepcopy(saved[name])
            elif "default" in field:
                current[name] = deepcopy(field["default"])
        values[identifier] = current
    return values


class SettingsReadHostFactoryV4:
    """Bind UI preferences and model reads to a reviewed Host capture."""

    def __init__(self, function_id: str = FUNCTION_ID) -> None:
        self.function_id = function_id

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Prepare one exact read operation without reading or creating state."""
        if not context.profile_id or context.user_data_root is None:
            raise PermissionError("settings capture is incomplete")
        if len(context.provider_bindings) != 1:
            raise PermissionError("settings binding is ambiguous")
        operation_ids: set[str] = set()
        for binding in context.provider_bindings:
            operation = binding.operation
            if (
                binding.function.function_id != self.function_id
                or operation.contract_id != CONTRACT_ID
                or operation.operation_id
                != {
                    FUNCTION_ID: OPERATION_ID,
                    CATALOG_FUNCTION_ID: CATALOG_OPERATION_ID,
                }.get(self.function_id)
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
                allowed_contract_ids=frozenset({MODEL_CONTRACT, PRESENTATION_CONTRACT}),
                consumer_pack_id=PACK_ID,
            )
            models = client.invoke(
                MODEL_CONTRACT,
                MODEL_OPERATION,
                {"profile_id": context.profile_id, "operation": "list"},
            )
            presentation = client.invoke(
                PRESENTATION_CONTRACT, PRESENTATION_OPERATION,
                {"profile_id": context.profile_id, "kind": "ui", "model_options": _model_options(models)},
            )
            if not isinstance(presentation, Mapping) or set(presentation) != {"sections", "definitions"}:
                raise ValueError("application presentation is unavailable")
            sections = presentation["sections"]
            definitions = presentation["definitions"]
            if not isinstance(sections, list) or not isinstance(definitions, dict):
                raise ValueError("application presentation is invalid")
            settings = {"sections": sections, "values": _values(sections, store.read_snapshot())}
            if operation_id == OPERATION_ID:
                return settings
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


class PreferencesWriteHostFactoryV4:
    """Capture one finite display-preference mutation, not a generic settings API."""

    function_id = WRITE_FUNCTION_ID

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Bind persistence to the Host root and one write-only Function identity."""
        if not context.profile_id or context.user_data_root is None or len(context.provider_bindings) != 1:
            raise PermissionError("preferences write capture is incomplete")
        binding = context.provider_bindings[0]
        operation = binding.operation
        if (
            binding.function.function_id != WRITE_FUNCTION_ID
            or operation.contract_id != WRITE_CONTRACT_ID
            or operation.operation_id != WRITE_OPERATION_ID
        ):
            raise PermissionError("preferences write binding is invalid")
        domain_id = context.domain_ids.get(
            (WRITE_CONTRACT_ID, WRITE_OPERATION_ID, binding.principal_ref.value)
        )
        if domain_id is None:
            raise PermissionError("preferences write domain is unavailable")
        store = FrontendSettingsStore(
            context.user_data_root / "defaultspack" / "shared" / "frontend_settings.json"
        )
        allowed_fields = {
            section: frozenset(fields) for section, fields in _PREFERENCE_TYPES.items()
        }

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            # Admission and approval remain on this exact write Function's
            # Broker edge; the read Function cannot invoke this contribution.
            del invocation
            if (
                operation_id != WRITE_OPERATION_ID
                or payload.get("profile_id") != context.profile_id
                or set(payload) - {"profile_id", "changes", "expected_revision", "_session_id"}
                or not {"profile_id", "changes", "expected_revision"} <= set(payload)
            ):
                raise PermissionError("preferences write request is invalid")
            changes = payload["changes"]
            if not isinstance(changes, dict) or not changes:
                raise ValueError("preferences changes must be a nonempty object")
            for section, fields in changes.items():
                if section not in _PREFERENCE_TYPES or not isinstance(fields, dict) or not fields:
                    raise PermissionError("preferences section is not writable")
                for field, value in fields.items():
                    value_type = _PREFERENCE_TYPES[section].get(field)
                    if value_type is None:
                        raise PermissionError("preferences field is not writable")
                    if type(value) is not value_type or (isinstance(value, str) and len(value) > 2048):
                        raise ValueError("preferences value is invalid")
            return store.compare_and_swap_fields(
                changes, allowed_fields=allowed_fields,
                expected_revision=payload["expected_revision"],
            )

        return CapturedHostProviderV4(
            (HostProviderContributionV4(
                contract_id=WRITE_CONTRACT_ID,
                contract_version=operation.contract_version,
                operation_id=WRITE_OPERATION_ID,
                principal_id=binding.principal_ref.value,
                artifact_digest=binding.artifact.digest,
                implementation_digest=binding.function.implementation_digest,
                domain_id=domain_id,
                invoke=invoke,
            ),),
            lambda: None,
        )


HOST_PROVIDER_FACTORY = {
    FUNCTION_ID: SettingsReadHostFactoryV4(),
    CATALOG_FUNCTION_ID: SettingsReadHostFactoryV4(CATALOG_FUNCTION_ID),
    WRITE_FUNCTION_ID: PreferencesWriteHostFactoryV4(),
}
