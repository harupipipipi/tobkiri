"""Read the finite Command Protocol state projection from its canonical owner."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from ecosystem.tobkiri_ui_settings_pack.runtime.store import FrontendSettingsStore
from tobkiri_protocol.settings_state import settings_state_revision

FUNCTION_ID = "rumi_command_protocol_pack.command.state"
CONTRACT_ID = "tobkiri.resource.command.state.v1"
OPERATION_ID = "command.state.query"
STATE_REF = "defaultspack:models.deepthink_enabled"
_ALLOWED_FIELDS = frozenset({"profile_id", "state_refs", "_session_id"})


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


class CommandStateHostFactoryV4:
    """Capture one Profile-bound, read-only Command state provider."""

    function_id = FUNCTION_ID

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Capture without reading settings or creating state."""
        if (
            not context.profile_id
            or context.user_data_root is None
            or len(context.provider_bindings) != 1
        ):
            raise PermissionError("command state capture is incomplete")
        binding = context.provider_bindings[0]
        operation = binding.operation
        if (
            binding.function.function_id != FUNCTION_ID
            or operation.contract_id != CONTRACT_ID
            or operation.operation_id != OPERATION_ID
            or operation.contract_version != "1.0.0"
        ):
            raise PermissionError("command state binding is invalid")
        key = (CONTRACT_ID, OPERATION_ID, binding.principal_ref.value)
        domain_id = context.domain_ids.get(key)
        if domain_id is None:
            raise PermissionError("command state domain is unavailable")
        store = FrontendSettingsStore(
            context.user_data_root
            / "defaultspack"
            / "shared"
            / "frontend_settings.json"
        )

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
                raise PermissionError("command state request is invalid")
            _owner_identity(
                invocation.presentation_owner_principal_id,
                "presentation owner principal",
            )
            _owner_identity(
                invocation.presentation_owner_session_id,
                "presentation owner session",
            )
            refs = payload.get("state_refs")
            if (
                not isinstance(refs, list)
                or len(refs) > 16
                or any(ref != STATE_REF for ref in refs)
                or len(set(refs)) != len(refs)
            ):
                raise PermissionError("command state references are invalid")
            settings = store.read_snapshot()
            invocation.assert_current()
            models = settings.get("models")
            enabled = (
                models.get("deepthink_enabled")
                if isinstance(models, Mapping)
                else False
            )
            revision = settings_state_revision(settings, STATE_REF)
            if not isinstance(enabled, bool):
                raise ValueError("settings state is invalid")
            return {
                "api_version": "tobkiri.commands/v1",
                "states": [
                    {
                        "state_ref": STATE_REF,
                        "value": enabled,
                        "revision": revision,
                        "freshness": "authoritative",
                    }
                ] if refs else [],
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


HOST_PROVIDER_FACTORY = {FUNCTION_ID: CommandStateHostFactoryV4()}
