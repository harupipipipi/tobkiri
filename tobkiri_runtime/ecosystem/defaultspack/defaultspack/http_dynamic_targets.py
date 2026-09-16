"""Defaultspack's catalog-to-capability target projection.

The generic Host verifies every returned target against the captured Provider,
Function, artifact digest, Profile revision, Plan, and activation before it
can be used. This module only interprets Defaultspack's UI catalog shape.
"""

from __future__ import annotations

from typing import Mapping

from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPContractBinding,
    HTTPContractTarget,
)


def defaultspack_dynamic_capability_targets(
    binding: HTTPContractBinding,
    *,
    catalog: Mapping[str, object],
) -> tuple[HTTPContractTarget, ...]:
    """Build candidate targets for the Defaultspack capability invoke route."""

    if binding.path != "/api/ui/capability/invoke":
        return ()
    packs = catalog.get("packs")
    if not isinstance(packs, list):
        return ()
    targets: list[HTTPContractTarget] = []
    for pack in packs:
        if (
            not isinstance(pack, Mapping)
            or pack.get("enabled") is not True
            or pack.get("approved") is not True
        ):
            continue
        pack_id = str(pack.get("pack_id") or "").strip()
        artifact_digest = str(pack.get("artifact_digest") or "").strip()
        operations = pack.get("operations")
        if not pack_id or not artifact_digest or not isinstance(operations, list):
            continue
        for operation in operations:
            if not isinstance(operation, Mapping) or operation.get("invokable") is not True:
                continue
            contract_id = str(operation.get("contract_id") or "").strip()
            operation_id = str(operation.get("operation_id") or "").strip()
            provider_id = str(operation.get("provider_id") or "").strip()
            function_id = str(operation.get("function_id") or provider_id).strip()
            if not contract_id or not operation_id or not provider_id:
                continue
            targets.append(
                HTTPContractTarget(
                    contribution_id=f"pack.{pack_id}.{operation_id}",
                    contract_id=contract_id,
                    operation_id=operation_id,
                    provider_id=provider_id,
                    function_id=function_id,
                    allowed_payload_keys=_payload_keys(contract_id, operation_id),
                    owner_pack_id=pack_id,
                    artifact_digest=artifact_digest,
                )
            )
    return tuple(
        sorted(
            targets,
            key=lambda target: (
                target.owner_pack_id,
                target.contract_id,
                target.operation_id,
            ),
        )
    )


def _payload_keys(contract_id: str, operation_id: str) -> frozenset[str]:
    """Return the finite payload schema admitted by the generic UI bridge."""

    if contract_id == "tobkiri.service.media.inspect.v1":
        return frozenset(
            {"name", "path", "encoding", "max_bytes", "start_line", "end_line"}
        )
    if contract_id == "tobkiri.acceptance.packvm.sandbox.v1":
        prefix = "tobkiri_packvm_sandbox_qa_pack."
        scenarios = {
            "probe_isolation",
            "stdin_overflow",
            "stdout_overflow",
            "stderr_overflow",
            "deadline_hold",
            "cancel_hold",
            "abnormal_exit",
        }
        scenario = operation_id.removeprefix(prefix)
        if operation_id.startswith(prefix) and scenario in scenarios:
            if scenario == "stdin_overflow":
                return frozenset({"nonce", "fill"})
            return frozenset({"nonce"})
    if contract_id == "tobkiri.workflow.v4":
        workflow_payloads = {
            "definition.list": frozenset(),
            "definition.get": frozenset({"definition_id"}),
            "definition.create": frozenset({"definition_id", "document"}),
            "definition.update": frozenset(
                {"definition_id", "document", "if_match"}
            ),
            "definition.delete": frozenset({"definition_id", "if_match"}),
            "definition.validate": frozenset({"document"}),
            "definition.publish": frozenset({"definition_id", "if_match"}),
            "operation.palette": frozenset(),
        }
        return workflow_payloads.get(operation_id, frozenset())
    return frozenset()


__all__ = ["defaultspack_dynamic_capability_targets"]
