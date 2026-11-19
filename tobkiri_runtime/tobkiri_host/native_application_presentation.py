"""Request-scoped backend for the original Application presentation operation."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.invocation_scope_v4 import assert_dispatched_invocation
from core_runtime.native_provider_prepare_ipc import APPLICATION_PRESENTATION_MINIMUM_MEMORY
from tobkiri_protocol.canonical import canonical_digest

from .backends import BackendStatus, REQUIRED_PRODUCTION_GATES
from .broker import RequestEnvelope
from .contracts import ResolvedOperationBinding
from .effects import ProviderOutcome
from .errors import BackendUnavailableError
from .models import ExecutionKind, OpaqueAuthorityRef, RuntimeEvidence, require_digest


@dataclass
class _Reservation:
    envelope: RequestEnvelope | None = None
    active: bool = False


class NativeApplicationPresentationBackend:
    """Run a selected presentation edge through the managed Launcher."""

    memory_reservation_bytes = APPLICATION_PRESENTATION_MINIMUM_MEMORY

    def __init__(
        self,
        binding: ResolvedOperationBinding,
        caller: OpaqueAuthorityRef,
        domain_id: str,
        manifest_sha256: str,
        *,
        authority_store: AuthorityStore,
        execute: Callable[
            [RequestEnvelope, Callable[[], None]], dict[str, Any]
        ],
        capture_current: Callable[[RequestEnvelope], Callable[[], None]],
    ) -> None:
        require_digest(manifest_sha256, "admitted Application presentation manifest")
        self._binding = binding
        self._caller = caller
        self._domain = OpaqueAuthorityRef(domain_id)
        self._manifest_sha256 = manifest_sha256
        self._authority_store = authority_store
        self._execute = execute
        self._capture_current = capture_current
        self._reservations: dict[str, _Reservation] = {}
        self._requests: dict[str, _Reservation] = {}
        self._terminated = False
        self._lock = threading.Lock()
        backend_id = "tobkiri.native-application-presentation-v1"
        self.status = BackendStatus(
            backend_id=backend_id,
            execution_kind=ExecutionKind.WASM,
            platform="portable",
            backend_digest=canonical_digest({
                "backend": backend_id,
                "manifest_sha256": manifest_sha256,
                "caller": caller.value,
                "target": binding.principal_ref.value,
                "domain": domain_id,
                "variant_digest": binding.variant.digest,
            }),
            production_enabled=True,
            conformance_only=False,
            satisfied_gates=REQUIRED_PRODUCTION_GATES,
        )

    def materialize(
        self, binding: ResolvedOperationBinding, reservation_id: str,
    ) -> RuntimeEvidence:
        """Enroll a Broker reservation against this captured Wasm identity."""
        if binding != self._binding or not reservation_id:
            raise BackendUnavailableError("native Application presentation binding changed")
        with self._lock:
            if self._terminated or reservation_id in self._reservations:
                raise BackendUnavailableError(
                    "native Application presentation reservation is unavailable"
                )
            self._reservations[reservation_id] = _Reservation()
        return RuntimeEvidence(
            domain_ref=self._domain,
            executable_digest=self._manifest_sha256,
            backend_digest=self.status.backend_digest,
            authenticated_channel=True,
            nonce_fresh=True,
            isolation_profile=binding.route.execution_domain_profile,
            resource_reservation_id=reservation_id,
            guest_artifact_identity=self._manifest_sha256,
            guest_execution_boundary="wasm-component",
        )

    def invoke(self, request: object) -> ProviderOutcome:
        """Execute with the actual child lease and original request budget."""
        operation = self._binding.operation
        if (
            not isinstance(request, RequestEnvelope)
            or request.context.caller_principal != self._caller
            or request.target_principal != self._binding.principal_ref
            or request.target_domain != self._domain
            or request.context.target_domain_id != self._domain.value
            or request.context.target_backend_digest != self.status.backend_digest
            or (request.contract_id, request.contract_version, request.operation_id)
            != (operation.contract_id, operation.contract_version, operation.operation_id)
        ):
            raise BackendUnavailableError("native Application presentation request changed")
        request_id = request.context.request_id
        with self._lock:
            reservation = self._reservations.get(request.resource_reservation_id or "")
            if (
                self._terminated or reservation is None or reservation.envelope is not None
                or request_id in self._requests
            ):
                raise BackendUnavailableError(
                    "native Application presentation request is not enrolled"
                )
            reservation.envelope = request
            reservation.active = True
            self._requests[request_id] = reservation
        try:
            assert_dispatched_invocation(request, self._authority_store)
            assert_current = self._capture_current(request)

            def guard() -> None:
                assert_current()
                assert_dispatched_invocation(request, self._authority_store)

            guard()
            payload = self._execute(request, guard)
            guard()
            if not isinstance(payload, Mapping):
                raise BackendUnavailableError(
                    "native Application presentation returned a non-object"
                )
            return ProviderOutcome(payload)
        finally:
            with self._lock:
                reservation.active = False

    def cancel(self, request_id: str) -> None:
        """Signal the exact enrolled invocation and its IPC guard."""
        with self._lock:
            reservation = self._requests.get(request_id)
            if reservation is None or reservation.envelope is None:
                raise BackendUnavailableError(
                    "native Application presentation cancellation is unowned"
                )
            reservation.envelope.cancellation_requested.set()

    def release_materialization(self, reservation_id: str) -> None:
        """Release a reservation after its authenticated IPC has drained."""
        with self._lock:
            reservation = self._reservations.get(reservation_id)
            if reservation is None:
                return
            envelope = reservation.envelope
            if envelope is not None:
                if reservation.active:
                    envelope.cancellation_requested.set()
                    raise BackendUnavailableError(
                        "native Application presentation has not drained"
                    )
                del self._requests[envelope.context.request_id]
            del self._reservations[reservation_id]

    def terminate(self, domain_id: str) -> None:
        """Fence this captured execution domain and signal its invocations."""
        if domain_id != self._domain.value:
            raise BackendUnavailableError("native Application presentation domain changed")
        with self._lock:
            self._terminated = True
            for reservation in self._requests.values():
                if reservation.active and reservation.envelope is not None:
                    reservation.envelope.cancellation_requested.set()
