"""Read the sealed command catalog without importing any command executor."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)

FUNCTION_ID = "rumi_command_protocol_pack.catalog.read"
CONTRACT_ID = "tobkiri.resource.command.catalog.v1"
OPERATION_ID = "command.catalog.read"
PRESENTATION_CONTRACT = "tobkiri.resource.application.presentation.v1"
PRESENTATION_OPERATION = "defaultspack.presentation.read"
_APPROVAL_COMMANDS = {
    "host:request_commit_approval", "host:request_push_approval",
    "host:request_terminal_approval", "host:request_patch_approval",
    "host:request_restore_approval",
}
_HIGH_RISK_TARGET = (
    "rumi_command_protocol_pack.high-risk-command.service",
    "tobkiri.service.command.high-risk.v1",
    "high_risk_command.manage",
)


def _catalog(definitions: Mapping[str, Any], high_risk_available: bool) -> dict[str, Any]:
    if set(definitions) != {"commands", "diagnostics", "pack_generation"}:
        raise ValueError("command presentation fields are invalid")
    generation = definitions["pack_generation"]
    if type(generation) is not int or generation < 1:
        raise ValueError("command presentation generation is invalid")
    commands = deepcopy(definitions["commands"])
    diagnostics = deepcopy(definitions["diagnostics"])
    if not isinstance(commands, list) or len(commands) > 256 or not isinstance(diagnostics, list):
        raise ValueError("command presentation collections are invalid")
    for command in commands:
        if not isinstance(command, dict) or not isinstance(command.get("execution"), dict):
            raise ValueError("command presentation record is invalid")
        operation_ref = command["execution"].get("operation_ref")
        if operation_ref in _APPROVAL_COMMANDS:
            # UI metadata cannot weaken the Host owner's approval requirements.
            command["authorization"] = {
                "risk": "high", "permissions": ["host.process.exec_guarded"],
                "approval_required": True, "approval_policy": "required",
                "executor_policy_ref": "tobkiri.command.human_approved",
            }
        command["availability"] = (
            {"status": "available"}
            if high_risk_available and operation_ref in _APPROVAL_COMMANDS else {
                "status": "unavailable",
                "reason_code": "canonical_binding_missing",
                "reason": "This command's canonical execution binding is not connected.",
            }
        )
    serialized = json.dumps(commands, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    result = {
        "api_version": "tobkiri.commands/v1",
        "kind": "ResolvedCommandCatalog",
        "catalog_revision": hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16],
        "pack_generations": {"defaultspack": generation},
        "rollout": {
            "feature_flag": "command_protocol_v1",
            "phase": "enforced",
            "legacy_execution_enabled": False,
        },
        "commands": commands,
        # No state/datasource providers are bound by this read-only adapter.
        # Unconnected commands remain visible as unavailable, not executable.
        "states": [],
        "datasources": [],
        "state_snapshots": [],
        "diagnostics": diagnostics,
    }
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "command-protocol-v1.schema.json"
    Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8"))).validate(result)
    return result


class CommandCatalogHostFactoryV4:
    """Expose one exact command-catalog read, with no invocation side effects."""

    function_id = FUNCTION_ID

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Capture the reader and the available approval adapter identity."""
        if not context.profile_id or len(context.provider_bindings) != 1:
            raise PermissionError("command catalog capture is incomplete")
        binding = context.provider_bindings[0]
        operation = binding.operation
        if (
            binding.function.function_id != FUNCTION_ID
            or operation.contract_id != CONTRACT_ID
            or operation.operation_id != OPERATION_ID
        ):
            raise PermissionError("command catalog binding is invalid")
        domain_id = context.domain_ids.get((CONTRACT_ID, OPERATION_ID, binding.principal_ref.value))
        if domain_id is None:
            raise PermissionError("command catalog domain is unavailable")
        high_risk_available = any(
            (item.function.function_id, item.operation.contract_id, item.operation.operation_id)
            == _HIGH_RISK_TARGET
            for item in context.catalog_bindings
        )

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if (
                operation_id != OPERATION_ID
                or payload.get("profile_id") != context.profile_id
                or set(payload) - {"profile_id", "_session_id"}
            ):
                raise PermissionError("command catalog request is invalid")
            client = invocation.contract_client(
                allowed_contract_ids=frozenset({PRESENTATION_CONTRACT}),
                consumer_pack_id="rumi_command_protocol_pack",
            )
            definitions = client.invoke(
                PRESENTATION_CONTRACT, PRESENTATION_OPERATION,
                {"profile_id": context.profile_id, "kind": "commands"},
            )
            if not isinstance(definitions, Mapping):
                raise ValueError("command presentation is unavailable")
            return _catalog(definitions, high_risk_available)

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


HOST_PROVIDER_FACTORY = {FUNCTION_ID: CommandCatalogHostFactoryV4()}
