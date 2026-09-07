"""Read the sealed command catalog without importing any command executor."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from ecosystem.defaultspack.domain import frontend_command_catalog as projection

FUNCTION_ID = "rumi_command_protocol_pack.catalog.read"
CONTRACT_ID = "tobkiri.resource.command.catalog.v1"
OPERATION_ID = "command.catalog.read"
_HIGH_RISK_TARGET = (
    "rumi_command_protocol_pack.high-risk-command.service",
    "tobkiri.service.command.high-risk.v1",
    "high_risk_command.manage",
)


def _catalog(high_risk_available: bool) -> dict[str, Any]:
    root = Path(projection.__file__).resolve().parents[1]
    source = root / "commands" / "default_commands.json"
    schema_path = root / "schemas" / "command-protocol-v1.schema.json"
    source_commands = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(source_commands, list):
        raise ValueError("sealed command catalog must be a list")
    digest = hashlib.sha256()
    for path in (root / "pack.v4.json", source, schema_path):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    generation = max(1, int.from_bytes(digest.digest()[:4], "big"))
    reader = projection.CommandCatalogProjection()
    diagnostics = reader._identity_collisions(source_commands)
    commands = []
    for source_command in source_commands:
        command = reader._resolve_command(source_command, diagnostics, generation)
        operation_ref = command["execution"].get("operation_ref")
        if not (high_risk_available and operation_ref in projection.OPERATION_AUTHORITY):
            command["availability"] = {
                "status": "unavailable",
                "reason_code": "canonical_binding_missing",
                "reason": "This command's canonical execution binding is not connected.",
            }
        commands.append(command)
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
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(result)
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
            del invocation
            if (
                operation_id != OPERATION_ID
                or payload.get("profile_id") != context.profile_id
                or set(payload) - {"profile_id", "_session_id"}
            ):
                raise PermissionError("command catalog request is invalid")
            return _catalog(high_risk_available)

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
