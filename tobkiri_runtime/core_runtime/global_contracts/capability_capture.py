"""Host-owned verification for captured HTTP capability bindings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from tobkiri_protocol.canonical import canonical_digest

from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPCapabilitySnapshot,
    HTTPContractBinding,
    HTTPContractTarget,
)


class CapabilityDispatchSession(Protocol):
    """Finite dispatch evidence needed to bind one capability contribution."""

    @property
    def profile_id(self) -> str: ...

    @property
    def plan_digest(self) -> str: ...

    @property
    def profile_revision(self) -> str: ...

    @property
    def activation_id(self) -> str: ...

    def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, Any], ...]: ...

    def assert_operation_ready(self, contract_id: str, operation_id: str) -> None: ...


class DynamicCapabilityTargetFactory(Protocol):
    """Application-owned mapping from catalog metadata to candidate targets."""

    def __call__(
        self,
        binding: HTTPContractBinding,
        *,
        catalog: Mapping[str, object],
    ) -> tuple[HTTPContractTarget, ...]:
        """Return untrusted candidates for Host identity verification."""


@dataclass(frozen=True)
class CapabilityBindingSnapshot(HTTPCapabilitySnapshot):
    """Host-verified targets and catalog hash for an HTTP capability route."""

    def to_mapping(
        self,
        *,
        profile_id: str,
        profile_revision: str,
        activation_id: str,
        plan_digest: str,
    ) -> dict[str, object]:
        """Return the finite Host-injection payload consumed by read models."""

        return {
            "profile_id": profile_id,
            "profile_revision": profile_revision,
            "activation_id": activation_id,
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
    binding: HTTPContractBinding,
    *,
    session: CapabilityDispatchSession,
    catalog: Mapping[str, object],
    dynamic_target_factory: DynamicCapabilityTargetFactory | None = None,
) -> CapabilityBindingSnapshot:
    """Capture enabled contributions through the same Host evidence as invoke."""

    targets: list[HTTPContractTarget] = []
    for target in binding.targets:
        static_target = _capture_static_target(target, session=session)
        if static_target is not None:
            targets.append(static_target)
    if dynamic_target_factory is not None:
        dynamic = [
            captured
            for target in dynamic_target_factory(binding, catalog=catalog)
            if (captured := _capture_static_target(target, session=session)) is not None
        ]
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
                "profile_revision": session.profile_revision,
                "activation_id": session.activation_id,
                "plan_digest": session.plan_digest,
                "contributions": [_target_digest_payload(target) for target in captured_targets],
            }
        ),
        targets=captured_targets,
    )


def _capture_static_target(
    target: HTTPContractTarget,
    *,
    session: CapabilityDispatchSession,
) -> HTTPContractTarget | None:
    providers = tuple(
        item
        for item in session.provider_metadata(target.contract_id)
        if item.get("provider_id") == target.provider_id
        and item.get("function_id") == target.function_id
        and item.get("operation_id") == target.operation_id
        and item.get("profile_id") == session.profile_id
        and item.get("profile_revision") == session.profile_revision
        and item.get("activation_id") == session.activation_id
        and item.get("plan_digest") == session.plan_digest
    )
    if len(providers) != 1:
        return None
    artifact_digest = str(providers[0].get("artifact_digest") or "").strip()
    if not artifact_digest or (
        target.artifact_digest and target.artifact_digest != artifact_digest
    ):
        return None
    try:
        session.assert_operation_ready(target.contract_id, target.operation_id)
    except Exception:
        return None
    return HTTPContractTarget(
        contribution_id=target.contribution_id,
        contract_id=target.contract_id,
        operation_id=target.operation_id,
        provider_id=target.provider_id,
        function_id=target.function_id,
        allowed_payload_keys=target.allowed_payload_keys,
        owner_pack_id=target.owner_pack_id,
        artifact_digest=artifact_digest,
    )


def _target_digest_payload(target: HTTPContractTarget) -> dict[str, str]:
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
    "DynamicCapabilityTargetFactory",
    "capture_capability_binding_snapshot",
]
