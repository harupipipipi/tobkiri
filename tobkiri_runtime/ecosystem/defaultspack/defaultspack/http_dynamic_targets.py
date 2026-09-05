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
                    allowed_payload_keys=_payload_keys(contract_id),
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


def _payload_keys(contract_id: str) -> frozenset[str]:
    if contract_id == "tobkiri.service.media.inspect.v1":
        return frozenset(
            {"name", "path", "encoding", "max_bytes", "start_line", "end_line"}
        )
    return frozenset()


__all__ = ["defaultspack_dynamic_capability_targets"]
