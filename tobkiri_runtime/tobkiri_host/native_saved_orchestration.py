"""Run the original Saved Orchestration ABI in the Launcher Wasm component."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.saved_conversation import (
    MAX_SAVED_CHAIN_BYTES,
    MAX_SAVED_TURN_LIFETIME_SECONDS,
    SAVED_CONVERSATION_CONTRACT,
    SAVED_CONVERSATION_OPERATION,
    validate_saved_conversation_input,
)
from tobkiri_protocol.saved_tools import MAX_SAVED_TOOL_HOPS

from .backends import REQUIRED_PRODUCTION_GATES, BackendStatus
from .broker import NestedCancellationProof, RequestEnvelope
from .continuation_chain import ChainIdentity, ContinuationChains
from .contracts import ResolvedOperationBinding
from .effects import ProviderOutcome
from .errors import BackendUnavailableError, RequestCancellationRequestedError
from .models import ExecutionKind, OpaqueAuthorityRef, RuntimeEvidence, require_digest
from .operation_cancellation import (
    _active_nested_cancellation_proof,
    _NestedCancellationProof,
)
from .saved_guest_dispatch import INVOKE_RESULT, SavedGuestTurns
from .saved_host_exchange import SavedHostExchange
from .saved_rejection import saved_turn_rejection
from .saved_turn_plan import SavedToolFrame

NATIVE_SAVED_ORCHESTRATION_BACKEND = "tobkiri.native-saved-orchestration-v1"


@dataclass
class _Reservation:
    envelope: RequestEnvelope | None = None
    active: bool = False


class NativeSavedOrchestrationBackend:
    """Use a verified component for one exact signed logical Saved binding.

    The composition root supplies the authenticated Launcher IPC and the same
    captured preflight and dispatch callbacks as the PackVM backend. Launcher
    owns and accounts for the component worker separately. This backend owns
    the Host step and continuation state until the Broker drains the request.
    """

    memory_reservation_bytes = 10 * 1024 * 1024

    def __init__(
        self,
        binding: ResolvedOperationBinding,
        domain_id: str,
        manifest_sha256: str,
        *,
        execute: Callable[
            [RequestEnvelope, Mapping[str, Any], Callable[[], None]],
            Mapping[str, Any],
        ],
        preflight: Callable[[RequestEnvelope, NestedCancellationProof], None],
        dispatch: Callable[
            [RequestEnvelope, SavedToolFrame, NestedCancellationProof],
            Mapping[str, Any],
        ],
        capture_current: Callable[[RequestEnvelope], Callable[[], None]],
    ) -> None:
        operation = binding.operation
        if (
            binding.artifact.pack_id != "tobkiri_conversation_orchestration_pack"
            or binding.function.function_id
            != "tobkiri_conversation_orchestration_pack.saved"
            or binding.variant.execution_kind is not ExecutionKind.PACK_VM
            or (
                operation.contract_id,
                operation.contract_version,
                operation.operation_id,
            )
            != (SAVED_CONVERSATION_CONTRACT, "1.0.0", SAVED_CONVERSATION_OPERATION)
        ):
            raise ValueError("native Saved Orchestration logical binding is invalid")
        require_digest(manifest_sha256, "native Saved Orchestration manifest")
        self._binding = binding
        self._domain = OpaqueAuthorityRef(domain_id)
        self._manifest_sha256 = manifest_sha256
        self._execute = execute
        self._preflight = preflight
        self._dispatch = dispatch
        self._capture_current = capture_current
        self._reservations: dict[str, _Reservation] = {}
        self._requests: dict[str, _Reservation] = {}
        self._terminated = False
        self._lock = threading.Lock()
        route = asdict(binding.route)
        route["adapter_ids"] = list(binding.route.adapter_ids)
        self.status = BackendStatus(
            backend_id=NATIVE_SAVED_ORCHESTRATION_BACKEND,
            execution_kind=ExecutionKind.WASM,
            platform="portable",
            backend_digest=canonical_digest(
                {
                    "backend": NATIVE_SAVED_ORCHESTRATION_BACKEND,
                    "manifest_sha256": manifest_sha256,
                    "principal": binding.principal_ref.value,
                    "domain": domain_id,
                    "route": route,
                    "implementation_digest": binding.function.implementation_digest,
                    "variant_digest": binding.variant.digest,
                    "contract_revision": operation.revision_digest,
                }
            ),
            production_enabled=True,
            conformance_only=False,
            satisfied_gates=REQUIRED_PRODUCTION_GATES,
        )

    def materialize(
        self,
        binding: ResolvedOperationBinding,
        reservation_id: str,
    ) -> RuntimeEvidence:
        """Reserve one Host request and report the authenticated Wasm identity."""
        if (
            binding != self._binding
            or not isinstance(reservation_id, str)
            or not reservation_id
        ):
            raise BackendUnavailableError(
                "native Saved Orchestration binding is invalid"
            )
        with self._lock:
            if self._terminated or reservation_id in self._reservations:
                raise BackendUnavailableError(
                    "native Saved Orchestration reservation is unavailable"
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
        """Run a saved exchange within the exact Broker reservation and proof."""
        if not isinstance(request, RequestEnvelope):
            raise BackendUnavailableError(
                "native Saved Orchestration request is invalid"
            )
        reservation_id, request_id = (
            request.resource_reservation_id,
            request.context.request_id,
        )
        operation = self._binding.operation
        if (
            not isinstance(reservation_id, str)
            or not isinstance(request_id, str)
            or not 0 < len(request_id) <= 160
            or type(request.cancellation_requested) is not threading.Event
            or request.target_principal != self._binding.principal_ref
            or request.target_domain != self._domain
            or request.context.target_domain_id != self._domain.value
            or request.context.target_backend_digest != self.status.backend_digest
            or (request.contract_id, request.contract_version, request.operation_id)
            != (
                operation.contract_id,
                operation.contract_version,
                operation.operation_id,
            )
        ):
            raise BackendUnavailableError(
                "native Saved Orchestration request identity mismatch"
            )
        with self._lock:
            reservation = self._reservations.get(reservation_id)
            if (
                self._terminated
                or reservation is None
                or reservation.envelope is not None
                or request_id in self._requests
            ):
                raise BackendUnavailableError(
                    "native Saved Orchestration request is not enrolled"
                )
            reservation.envelope = request
            reservation.active = True
            self._requests[request_id] = reservation
        try:
            return self._invoke_enrolled(request, reservation_id)
        finally:
            with self._lock:
                reservation.active = False

    def _invoke_enrolled(
        self,
        request: RequestEnvelope,
        reservation_id: str,
    ) -> ProviderOutcome:
        proof = _active_nested_cancellation_proof.get()
        if type(proof) is not _NestedCancellationProof:
            raise BackendUnavailableError(
                "native Saved Orchestration proof is unavailable"
            )
        deadline = request.deadline_monotonic
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise BackendUnavailableError(
                "native Saved Orchestration deadline is invalid"
            )
        deadline = min(deadline, time.monotonic() + MAX_SAVED_TURN_LIFETIME_SECONDS)

        def check_budget() -> None:
            if request.cancellation_requested.is_set():
                raise RequestCancellationRequestedError(
                    "saved request cancellation was requested"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError("saved request deadline expired")

        def check_proof() -> None:
            check_budget()
            if not proof.matches_invocation(
                request,
                proof._owner_principal,
                proof._owner_session,
            ):
                raise BackendUnavailableError(
                    "native Saved Orchestration proof identity mismatch"
                )

        check_proof()
        assert_current = self._capture_current(request)

        def guard() -> None:
            check_proof()
            assert_current()
            check_budget()

        guard()
        payload = validate_saved_conversation_input(request.payload)
        guard()
        try:
            if self._preflight(request, proof) is not None:
                raise BackendUnavailableError(
                    "native Saved Orchestration preflight acknowledgement is invalid"
                )
        except Exception as exc:
            raise saved_turn_rejection(
                exc,
                reason="native Saved Orchestration preflight rejected request",
                default="SAVED_REQUEST_REJECTED",
            ) from exc
        guard()

        domain = self._domain.value
        request_id = request.context.request_id
        binding_digest = canonical_digest(
            {
                "backend_digest": self.status.backend_digest,
                "reservation_id": reservation_id,
                "request_id": request_id,
                "request_digest": request.request_digest,
            }
        )
        transport = {
            "request_id": request_id,
            "target_domain": domain,
            "guest_artifact_identity": self._manifest_sha256,
            "request_digest": request.request_digest,
            "deadline_monotonic": format(request.deadline_monotonic, ".17g"),
            "payload": payload,
        }
        exchange = SavedHostExchange(
            ChainIdentity(domain, request_id, binding_digest, deadline),
            request_digest=request.request_digest,
            artifact_identity=self._manifest_sha256,
            deadline_text=transport["deadline_monotonic"],
            chains=ContinuationChains(
                max_hops=MAX_SAVED_TOOL_HOPS,
                max_bytes=MAX_SAVED_CHAIN_BYTES,
                max_lifetime=MAX_SAVED_TURN_LIFETIME_SECONDS,
            ),
            request=payload["request"],
        )
        guest = SavedGuestTurns()

        def execute(
            _transport: dict[str, Any],
            step_input: dict[str, Any],
            _deadline: float,
            guest_guard: Callable[[], None],
        ) -> dict[str, Any]:
            def step_guard() -> None:
                guest_guard()
                guard()

            step_guard()
            outcome = self._execute(request, step_input, step_guard)
            step_guard()
            if outcome.get("kind") == "tobkiri.packvm.continuation.intent.v2":
                return dict(outcome)
            return {"kind": INVOKE_RESULT, "outcome": dict(outcome)}

        try:
            pending = guest.begin(transport, binding_digest, execute)
            for _ in range(exchange.maximum_hops):
                guard()
                frame = exchange.accept(pending["host_bridge_request"])
                callback_frame = exchange.callback_frame(frame)
                guard()
                outcome = self._dispatch(request, callback_frame, proof)
                guard()
                result = exchange.result(outcome)
                pending = guest.resume(domain, request_id, result, execute)
                guard()
                if pending.get("state") != "pending":
                    final = pending["outcome"]
                    exchange.finish(final)
                    guard()
                    return ProviderOutcome(final)
            raise BackendUnavailableError(
                "native Saved Orchestration exceeds its action bound"
            )
        except BaseException:
            exchange.cancel()
            guest.cancel(domain, request_id)
            raise

    def cancel(self, request_id: str) -> None:
        """Signal the exact enrolled request, including the Launcher IPC guard."""
        with self._lock:
            reservation = self._requests.get(request_id)
            if reservation is None or reservation.envelope is None:
                raise BackendUnavailableError(
                    "native Saved Orchestration cancellation is unowned"
                )
            reservation.envelope.cancellation_requested.set()

    def release_materialization(self, reservation_id: str) -> None:
        """Release only after the enrolled invocation has returned from IPC."""
        with self._lock:
            reservation = self._reservations.get(reservation_id)
            if reservation is None:
                return
            envelope = reservation.envelope
            if reservation.active and envelope is not None:
                envelope.cancellation_requested.set()
                raise BackendUnavailableError(
                    "native Saved Orchestration has not drained"
                )
            if envelope is not None:
                self._requests.pop(envelope.context.request_id)
            del self._reservations[reservation_id]

    def terminate(self, domain_id: str) -> None:
        """Fence this captured domain and signal each of its active invocations."""
        if domain_id != self._domain.value:
            raise BackendUnavailableError(
                "native Saved Orchestration domain is unowned"
            )
        with self._lock:
            self._terminated = True
            for reservation in self._reservations.values():
                if reservation.active and reservation.envelope is not None:
                    reservation.envelope.cancellation_requested.set()
