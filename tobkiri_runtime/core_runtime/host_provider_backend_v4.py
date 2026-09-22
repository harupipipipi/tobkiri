"""Exact Function-principal routing for captured Host Provider contributions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from tobkiri_host.backends import BackendStatus, REQUIRED_PRODUCTION_GATES
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.contracts import ResolvedOperationBinding
from tobkiri_host.effects import ProviderOutcome
from tobkiri_host.errors import AuthorizationError
from tobkiri_host.models import (
    ExecutionKind,
    OpaqueAuthorityRef,
    RuntimeEvidence,
)
from tobkiri_protocol.canonical import canonical_digest


class HostProviderInvocationContextV4(Protocol):
    """Restricted Host capabilities bound to one authenticated invocation."""

    @property
    def envelope(self) -> RequestEnvelope:
        """Return the Broker-authenticated envelope for this invocation."""

    def contract_client(
        self,
        *,
        allowed_contract_ids: frozenset[str],
        consumer_pack_id: str,
    ) -> Any:
        """Build a client restricted to declared contracts and this envelope."""


@dataclass(frozen=True)
class HostProviderContributionV4:
    """One callable bound to an exact resolved Function operation."""

    contract_id: str
    contract_version: str
    operation_id: str
    principal_id: str
    artifact_digest: str
    implementation_digest: str
    domain_id: str
    invoke: Callable[
        [str, Mapping[str, Any], HostProviderInvocationContextV4],
        Mapping[str, Any],
    ]

    @property
    def key(self) -> tuple[str, str, str]:
        """Return the immutable dispatch key."""
        return self.contract_id, self.operation_id, self.principal_id


@dataclass(frozen=True)
class HostProviderCaptureContextV4:
    """Host-owned immutable inputs supplied to a built-in Provider hook."""

    profile_id: str
    plan_digest: str
    security_epoch: int
    activation: Mapping[str, Any]
    state_root: Path
    provider_bindings: tuple[ResolvedOperationBinding, ...]
    catalog_bindings: tuple[ResolvedOperationBinding, ...]
    domain_ids: Mapping[tuple[str, str, str], str]
    user_data_root: Path | None = None


@dataclass(frozen=True)
class CapturedHostProviderV4:
    """Contributions and resources owned by one captured Provider hook."""

    contributions: tuple[HostProviderContributionV4, ...]
    close: Callable[[], None]


class HostProviderFactoryV4(Protocol):
    """Static Host-TCB hook for one exact Function identity."""

    function_id: str

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Capture contributions from verified resolved bindings."""


class ExactHostProviderBackendV4:
    """Route a shared Host substrate by exact resolved Function principal."""

    def __init__(
        self,
        contributions: tuple[HostProviderContributionV4, ...],
        *,
        backend_id: str,
        profile_id: str,
        plan_digest: str,
        security_epoch: int,
        invocation_context: Callable[
            [RequestEnvelope], HostProviderInvocationContextV4
        ],
    ) -> None:
        if not contributions:
            raise ValueError("Host Provider backend requires contributions")
        self._contributions = {item.key: item for item in contributions}
        if len(self._contributions) != len(contributions):
            raise ValueError("duplicate Host Provider contribution")
        self._invocation_context = invocation_context
        backend_digest = canonical_digest(
            {
                "backend": "tobkiri.exact-host-provider.v4",
                "backend_id": backend_id,
                "profile_id": profile_id,
                "plan_digest": plan_digest,
                "security_epoch": security_epoch,
                "contributions": [
                    {
                        "contract_id": item.contract_id,
                        "contract_version": item.contract_version,
                        "operation_id": item.operation_id,
                        "principal_id": item.principal_id,
                        "artifact_digest": item.artifact_digest,
                        "implementation_digest": item.implementation_digest,
                        "domain_id": item.domain_id,
                    }
                    for item in sorted(contributions, key=lambda value: value.key)
                ],
            }
        )
        self.status = BackendStatus(
            backend_id=backend_id,
            execution_kind=ExecutionKind.HOST_EXTENSION,
            platform="any-any",
            backend_digest=backend_digest,
            production_enabled=True,
            conformance_only=False,
            satisfied_gates=REQUIRED_PRODUCTION_GATES,
        )

    def supports(self, binding: ResolvedOperationBinding) -> bool:
        """Return true only for one completely matching contribution."""
        contribution = self._contribution(binding)
        return contribution is not None

    def materialize(
        self,
        binding: ResolvedOperationBinding,
        reservation_id: str,
    ) -> RuntimeEvidence:
        """Produce evidence for the exact contribution and no other target."""
        contribution = self._contribution(binding)
        if not reservation_id or contribution is None:
            raise AuthorizationError("Host Provider contribution is unavailable")
        return RuntimeEvidence(
            domain_ref=OpaqueAuthorityRef(contribution.domain_id),
            executable_digest=contribution.implementation_digest,
            backend_digest=self.status.backend_digest,
            authenticated_channel=True,
            nonce_fresh=True,
        )

    def invoke(self, request: object) -> ProviderOutcome:
        """Invoke only an envelope matching the captured contribution."""
        if not isinstance(request, RequestEnvelope):
            raise AuthorizationError("Host Provider envelope is invalid")
        key = (
            request.contract_id,
            request.operation_id,
            request.target_principal.value,
        )
        contribution = self._contributions.get(key)
        if (
            contribution is None
            or request.contract_version != contribution.contract_version
            or request.target_domain.value != contribution.domain_id
        ):
            raise AuthorizationError("Host Provider envelope binding is invalid")
        return ProviderOutcome(
            dict(
                contribution.invoke(
                    request.operation_id,
                    request.payload,
                    self._invocation_context(request),
                )
            )
        )

    def cancel(self, request_id: str) -> None:
        """Accept cancellation; individual providers observe durable fences."""
        if not request_id:
            raise AuthorizationError("Host Provider cancellation ID is invalid")

    def terminate(self, domain_id: str) -> None:
        """Reject termination requests outside captured contribution domains."""
        if domain_id not in {item.domain_id for item in self._contributions.values()}:
            raise AuthorizationError("Host Provider domain is invalid")

    def _contribution(
        self,
        binding: ResolvedOperationBinding,
    ) -> HostProviderContributionV4 | None:
        contribution = self._contributions.get(
            (
                binding.operation.contract_id,
                binding.operation.operation_id,
                binding.principal_ref.value,
            )
        )
        if contribution is None:
            return None
        if (
            binding.operation.contract_version != contribution.contract_version
            or binding.artifact.digest != contribution.artifact_digest
            or binding.function.implementation_digest
            != contribution.implementation_digest
        ):
            return None
        return contribution


__all__ = [
    "CapturedHostProviderV4",
    "ExactHostProviderBackendV4",
    "HostProviderCaptureContextV4",
    "HostProviderContributionV4",
    "HostProviderFactoryV4",
    "HostProviderInvocationContextV4",
]
