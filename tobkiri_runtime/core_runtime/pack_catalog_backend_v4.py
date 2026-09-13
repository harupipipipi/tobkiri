"""Finite production backend for the Host Pack catalog read operation."""

from __future__ import annotations

from tobkiri_host.backends import (
    REQUIRED_PRODUCTION_GATES,
    BackendStatus,
)
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.contracts import ResolvedOperationBinding
from tobkiri_host.effects import ProviderOutcome
from tobkiri_host.models import ExecutionKind, OpaqueAuthorityRef, RuntimeEvidence

from .pack_control_v4 import (
    CONTROL_PRESENTATION_CONTRACT,
    PACK_CONTROL_CONTRACT,
    CapturedPackCatalogReader,
    CapturedPackControlSession,
    PackControlDenied,
)


PACK_CATALOG_BACKEND_ID = "tobkiri.python-host-v4"


class PackCatalogBackendV4:
    """Serve only the exact captured ``catalog.read`` Function principal."""

    def __init__(
        self,
        *,
        reader: CapturedPackCatalogReader,
        target_principal_id: str,
        implementation_digest: str,
        domain_id: str,
        backend_digest: str,
    ) -> None:
        self._reader = reader
        self._target_principal_id = target_principal_id
        self._implementation_digest = implementation_digest
        self._domain_id = domain_id
        self.status = BackendStatus(
            backend_id=PACK_CATALOG_BACKEND_ID,
            execution_kind=ExecutionKind.HOST_EXTENSION,
            platform="any-any",
            backend_digest=backend_digest,
            production_enabled=True,
            conformance_only=False,
            satisfied_gates=REQUIRED_PRODUCTION_GATES,
        )

    def materialize(
        self,
        binding: ResolvedOperationBinding,
        reservation_id: str,
    ) -> RuntimeEvidence:
        """Return Host evidence only for the pinned read-only Provider."""

        if (
            not reservation_id
            or binding.principal_ref.value != self._target_principal_id
            or binding.operation.contract_id != PACK_CONTROL_CONTRACT
            or binding.operation.operation_id != "catalog.read"
            or binding.function.implementation_digest != self._implementation_digest
        ):
            raise PackControlDenied("Pack catalog backend binding is unavailable")
        return RuntimeEvidence(
            domain_ref=OpaqueAuthorityRef(self._domain_id),
            executable_digest=self._implementation_digest,
            backend_digest=self.status.backend_digest,
            authenticated_channel=True,
            nonce_fresh=True,
        )

    def invoke(self, request: object) -> ProviderOutcome:
        """Read the catalog after exact Broker envelope validation."""

        if not isinstance(request, RequestEnvelope) or (
            request.target_principal.value != self._target_principal_id
            or request.target_domain.value != self._domain_id
            or request.contract_id != PACK_CONTROL_CONTRACT
            or request.operation_id != "catalog.read"
            or dict(request.payload)
        ):
            raise PackControlDenied("Pack catalog Provider envelope is invalid")
        return ProviderOutcome(self._reader.read())

    def supports(self, binding: ResolvedOperationBinding) -> bool:
        """Return true only for the captured read-only contribution."""
        return bool(
            binding.principal_ref.value == self._target_principal_id
            and binding.operation.contract_id == PACK_CONTROL_CONTRACT
            and binding.operation.operation_id == "catalog.read"
            and binding.function.implementation_digest == self._implementation_digest
        )

    def cancel(self, request_id: str) -> None:
        """Accept cancellation without exposing another operation."""

        del request_id

    def terminate(self, domain_id: str) -> None:
        """Accept a Host fence only for this exact Provider domain."""

        if domain_id != self._domain_id:
            raise PackControlDenied("Pack catalog Provider domain is invalid")


class PackControlBackendV4:
    """Execute only the Profile-selected finite Pack control operations."""

    def __init__(
        self,
        *,
        session: CapturedPackControlSession,
        targets: dict[tuple[str, str], tuple[str, str, str]],
        backend_digest: str,
    ) -> None:
        self._session = session
        self._targets = dict(targets)
        self.status = BackendStatus(
            backend_id=PACK_CATALOG_BACKEND_ID,
            execution_kind=ExecutionKind.HOST_EXTENSION,
            platform="any-any",
            backend_digest=backend_digest,
            production_enabled=True,
            conformance_only=False,
            satisfied_gates=REQUIRED_PRODUCTION_GATES,
        )

    def materialize(
        self,
        binding: ResolvedOperationBinding,
        reservation_id: str,
    ) -> RuntimeEvidence:
        """Return evidence only for an exact selected Function principal."""

        key = (binding.operation.contract_id, binding.operation.operation_id)
        expected = self._targets.get(key)
        if (
            not reservation_id
            or binding.operation.contract_id
            not in {PACK_CONTROL_CONTRACT, CONTROL_PRESENTATION_CONTRACT}
            or expected is None
            or expected[0] != binding.principal_ref.value
            or expected[1] != binding.function.implementation_digest
        ):
            raise PackControlDenied("Pack control backend binding is unavailable")
        return RuntimeEvidence(
            domain_ref=OpaqueAuthorityRef(expected[2]),
            executable_digest=expected[1],
            backend_digest=self.status.backend_digest,
            authenticated_channel=True,
            nonce_fresh=True,
        )

    def supports(self, binding: ResolvedOperationBinding) -> bool:
        """Return true only for an exact captured Pack control contribution."""
        key = (binding.operation.contract_id, binding.operation.operation_id)
        expected = self._targets.get(key)
        return bool(
            expected is not None
            and expected[0] == binding.principal_ref.value
            and expected[1] == binding.function.implementation_digest
        )

    def invoke(self, request: object) -> ProviderOutcome:
        """Invoke the finite Provider after exact Broker envelope validation."""

        if not isinstance(request, RequestEnvelope):
            raise PackControlDenied("Pack control Provider envelope is invalid")
        expected = self._targets.get((request.contract_id, request.operation_id))
        if (
            request.contract_id not in {PACK_CONTROL_CONTRACT, CONTROL_PRESENTATION_CONTRACT}
            or expected is None
            or request.target_principal.value != expected[0]
            or request.target_domain.value != expected[2]
        ):
            raise PackControlDenied("Pack control Provider envelope is invalid")
        result = self._session.invoke(
            request.contract_id,
            request.operation_id,
            {
                **dict(request.payload),
                "_session_id": request.context.caller_session_id,
            },
        )
        return ProviderOutcome(result)

    def cancel(self, request_id: str) -> None:
        """Accept Broker cancellation without creating another authority path."""

        del request_id

    def terminate(self, domain_id: str) -> None:
        """Accept a Host fence only for one captured Provider domain."""

        if domain_id not in {target[2] for target in self._targets.values()}:
            raise PackControlDenied("Pack control Provider domain is invalid")


__all__ = [
    "PACK_CATALOG_BACKEND_ID",
    "PackCatalogBackendV4",
    "PackControlBackendV4",
]
