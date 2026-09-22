"""Host-owned capability invocation bindings for the captured v4 runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from tobkiri_protocol.canonical import canonical_digest

from .frontend_contract_routes import FrontendContractBinding, FrontendContractTarget


class CapabilityDispatchSession(Protocol):
    """Finite dispatch evidence needed to bind one capability contribution."""

    @property
    def profile_id(self) -> str: ...

    @property
    def plan_digest(self) -> str: ...

    def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, Any], ...]: ...

    def assert_operation_ready(self, contract_id: str, operation_id: str) -> None: ...


@dataclass(frozen=True)
class CapabilityBindingSnapshot:
    """Exact invoke targets and the catalog hash accepted by PackAPI."""

    catalog_hash: str
    targets: tuple[FrontendContractTarget, ...]

    def to_mapping(self, *, profile_id: str, plan_digest: str) -> dict[str, object]:
        """Return the finite Host-injection payload consumed by read models."""

        return {
            "profile_id": profile_id,
            "plan_digest": plan_digest,
            "catalog_hash": self.catalog_hash,
            "targets": [
                {
                    **_target_digest_payload(target),
                    "owner_pack_id": target.owner_pack_id,
                }
                for target in self.targets
            ],
        }


def capture_capability_binding_snapshot(
    binding: FrontendContractBinding,
    *,
    session: CapabilityDispatchSession,
    catalog: Mapping[str, object],
) -> CapabilityBindingSnapshot:
    """Capture enabled contributions through the same Host evidence as invoke."""

    targets: list[FrontendContractTarget] = []
    for target in binding.targets:
        static_target = _capture_static_target(target, session=session)
        if static_target is not None:
            targets.append(static_target)
    packs = catalog.get("packs")
    if binding.path == "/api/ui/capability/invoke" and isinstance(packs, list):
        dynamic: list[FrontendContractTarget] = []
        for pack in packs:
            if (
                not isinstance(pack, Mapping)
                or pack.get("enabled") is not True
                or pack.get("approved") is not True
            ):
                continue
            pack_id = str(pack.get("pack_id") or "").strip()
            operations = pack.get("operations")
            if not pack_id or not isinstance(operations, list):
                continue
            for operation in operations:
                dynamic_target = _capture_operation_target(
                    pack_id,
                    pack,
                    operation,
                    session=session,
                )
                if dynamic_target is not None:
                    dynamic.append(dynamic_target)
        targets.extend(
            sorted(
                dynamic,
                key=lambda target: (
                    target.owner_pack_id,
                    target.contract_id,
                    target.operation_id,
                ),
            )
        )
    captured_targets = tuple(targets)
    return CapabilityBindingSnapshot(
        catalog_hash=canonical_digest(
            {
                "profile_id": session.profile_id,
                "plan_digest": session.plan_digest,
                "contributions": [_target_digest_payload(target) for target in captured_targets],
            }
        ),
        targets=captured_targets,
    )


def _capture_static_target(
    target: FrontendContractTarget,
    *,
    session: CapabilityDispatchSession,
) -> FrontendContractTarget | None:
    providers = tuple(
        item
        for item in session.provider_metadata(target.contract_id)
        if item.get("provider_id") == target.provider_id
        and item.get("function_id") == target.function_id
        and item.get("operation_id") == target.operation_id
        and item.get("profile_id") == session.profile_id
        and item.get("plan_digest") == session.plan_digest
    )
    if len(providers) != 1:
        return None
    artifact_digest = str(providers[0].get("artifact_digest") or "").strip()
    if not artifact_digest:
        return None
    try:
        session.assert_operation_ready(target.contract_id, target.operation_id)
    except Exception:
        return None
    return FrontendContractTarget(
        contribution_id=target.contribution_id,
        contract_id=target.contract_id,
        operation_id=target.operation_id,
        provider_id=target.provider_id,
        function_id=target.function_id,
        allowed_payload_keys=target.allowed_payload_keys,
        owner_pack_id=target.owner_pack_id,
        artifact_digest=artifact_digest,
    )


def _capture_operation_target(
    pack_id: str,
    pack: Mapping[str, object],
    operation: object,
    *,
    session: CapabilityDispatchSession,
) -> FrontendContractTarget | None:
    if not isinstance(operation, Mapping) or operation.get("invokable") is not True:
        return None
    contract_id = str(operation.get("contract_id") or "").strip()
    operation_id = str(operation.get("operation_id") or "").strip()
    provider_id = str(operation.get("provider_id") or "").strip()
    function_id = str(operation.get("function_id") or provider_id).strip()
    artifact_digest = str(pack.get("artifact_digest") or "").strip()
    if not contract_id or not operation_id or not provider_id or not artifact_digest:
        return None
    providers = tuple(
        item
        for item in session.provider_metadata(contract_id)
        if item.get("provider_id") == provider_id
        and item.get("function_id") == function_id
        and item.get("operation_id") == operation_id
        and item.get("profile_id") == session.profile_id
        and item.get("plan_digest") == session.plan_digest
        and item.get("artifact_digest") == artifact_digest
    )
    if len(providers) != 1:
        return None
    try:
        session.assert_operation_ready(contract_id, operation_id)
    except Exception:
        return None
    return FrontendContractTarget(
        contribution_id=f"pack.{pack_id}.{operation_id}",
        contract_id=contract_id,
        operation_id=operation_id,
        provider_id=provider_id,
        function_id=function_id,
        allowed_payload_keys=_dynamic_payload_keys(contract_id),
        owner_pack_id=pack_id,
        artifact_digest=artifact_digest,
    )


def _dynamic_payload_keys(contract_id: str) -> frozenset[str]:
    if contract_id == "tobkiri.service.media.inspect.v1":
        return frozenset({"name", "path", "encoding", "max_bytes", "start_line", "end_line"})
    return frozenset()


def _target_digest_payload(target: FrontendContractTarget) -> dict[str, str]:
    return {
        "contribution_id": target.contribution_id,
        "contract_id": target.contract_id,
        "operation_id": target.operation_id,
        "provider_id": target.provider_id,
        "function_id": target.function_id,
        "artifact_digest": target.artifact_digest,
    }


__all__ = [
    "CapabilityBindingSnapshot",
    "capture_capability_binding_snapshot",
]
