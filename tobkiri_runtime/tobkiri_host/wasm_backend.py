"""Request-scoped production backend for import-free Wasm components."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import math
import threading
import time
from typing import Callable

from tobkiri_protocol.canonical import canonical_digest

from .artifact_materialization import MaterializedPackArtifact
from .backends import BackendStatus, REQUIRED_PRODUCTION_GATES
from .broker import RequestEnvelope
from .contracts import ResolvedOperationBinding
from .effects import ProviderOutcome
from .errors import BackendUnavailableError, ProviderExecutionError
from .models import ExecutionKind, OpaqueAuthorityRef, RuntimeEvidence, require_digest
from .wasm_worker import ComponentWorker


WASMTIME_PULLEY_BACKEND = "tobkiri.wasmtime-pulley-v1"


@dataclass
class _ReservationWorker:
    binding: ResolvedOperationBinding
    domain_id: str
    worker: ComponentWorker
    artifact: bytes
    request_id: str | None = None


class WasmComponentBackend:
    """Run one verified component in one physically bounded worker process.

    The composition root supplies an authenticated worker command and its
    digest. The command is never accepted from a Pack, request, environment,
    or mutable manifest. Each Broker reservation owns exactly one worker.
    """

    def __init__(
        self,
        worker_command: tuple[str, ...],
        *,
        worker_command_digest: str,
        memory_reservation_bytes: int = 512 * 1024 * 1024,
        backend_id: str = WASMTIME_PULLEY_BACKEND,
    ) -> None:
        require_digest(worker_command_digest, "Wasm worker command")
        if worker_command_digest != canonical_digest(list(worker_command)):
            raise ValueError("Wasm worker command digest mismatch")
        if (
            type(memory_reservation_bytes) is not int
            or not 0 < memory_reservation_bytes <= 2 * 1024 * 1024 * 1024
        ):
            raise ValueError("Wasm worker memory reservation is invalid")
        # ComponentWorker validates the trusted executable and complete argv.
        probe = ComponentWorker(
            worker_command,
            rss_limit=memory_reservation_bytes,
        )
        probe.close()
        self._worker_command = tuple(worker_command)
        self.memory_reservation_bytes = memory_reservation_bytes
        self._artifact_resolver: Callable[
            [ResolvedOperationBinding], MaterializedPackArtifact
        ] | None = None
        self._target_domain_resolver: Callable[[ResolvedOperationBinding], str] | None = None
        self._reservations: dict[str, _ReservationWorker] = {}
        self._requests: dict[str, str] = {}
        self._lock = threading.RLock()
        self.status = BackendStatus(
            backend_id=backend_id,
            execution_kind=ExecutionKind.WASM,
            platform="portable",
            backend_digest=canonical_digest(
                {
                    "backend": backend_id,
                    "worker_command_digest": worker_command_digest,
                    "memory_reservation_bytes": memory_reservation_bytes,
                    "abi": "component-v1",
                    "engine": "wasmtime-pulley",
                    "wasi": False,
                }
            ),
            production_enabled=True,
            conformance_only=False,
            satisfied_gates=REQUIRED_PRODUCTION_GATES,
            enforces_platform=False,
            requires_platform_attestation=False,
        )

    def supports(self, binding: ResolvedOperationBinding) -> bool:
        """Accept only this backend's import-free component ABI."""

        return (
            binding.variant.execution_kind is ExecutionKind.WASM
            and binding.variant.backend == self.status.backend_id
            and binding.variant.runtime_abi == "component-v1"
        )

    def bind_artifact_resolver(
        self,
        resolver: Callable[[ResolvedOperationBinding], MaterializedPackArtifact],
    ) -> None:
        """Bind the activation-captured immutable artifact source once."""

        with self._lock:
            if self._reservations:
                raise BackendUnavailableError(
                    "Wasm artifact resolver cannot change after materialization"
                )
            if self._artifact_resolver is not None and self._artifact_resolver is not resolver:
                raise BackendUnavailableError("Wasm artifact resolver is already bound")
            self._artifact_resolver = resolver

    def bind_target_domain_resolver(
        self,
        resolver: Callable[[ResolvedOperationBinding], str],
    ) -> None:
        """Bind the exact Authority-owned domain resolver once."""

        with self._lock:
            if self._reservations:
                raise BackendUnavailableError(
                    "Wasm target domain resolver cannot change after materialization"
                )
            if (
                self._target_domain_resolver is not None
                and self._target_domain_resolver is not resolver
            ):
                raise BackendUnavailableError("Wasm target domain resolver is already bound")
            self._target_domain_resolver = resolver

    def materialize(
        self,
        binding: ResolvedOperationBinding,
        reservation_id: str,
    ) -> RuntimeEvidence:
        """Capture exact bytes and allocate one unstarted worker to a reservation."""

        if not self.supports(binding):
            raise BackendUnavailableError("Wasm binding does not match the backend")
        if not reservation_id:
            raise BackendUnavailableError("Wasm resource reservation is missing")
        resolver = self._artifact_resolver
        domain_resolver = self._target_domain_resolver
        if resolver is None or domain_resolver is None:
            raise BackendUnavailableError("Wasm production bindings are incomplete")
        try:
            artifact = resolver(binding)
            domain_id = domain_resolver(binding)
        except Exception as exc:
            raise BackendUnavailableError("Wasm materialization capture failed") from exc
        if (
            artifact.pack_id != binding.artifact.pack_id
            or artifact.artifact_digest != binding.artifact.digest
            or artifact.function_id != binding.function.function_id
            or artifact.implementation_digest != binding.function.implementation_digest
            or not isinstance(domain_id, str)
            or not domain_id
        ):
            raise BackendUnavailableError("Wasm materialization identity mismatch")
        implementation = next(
            item for item in artifact.files if item.path == artifact.implementation_path
        )
        worker = ComponentWorker(
            self._worker_command,
            rss_limit=self.memory_reservation_bytes,
        )
        with self._lock:
            if reservation_id in self._reservations:
                raise BackendUnavailableError("Wasm reservation is already materialized")
            self._reservations[reservation_id] = _ReservationWorker(
                binding=binding,
                domain_id=domain_id,
                worker=worker,
                artifact=implementation.content,
            )
        return RuntimeEvidence(
            domain_ref=OpaqueAuthorityRef(domain_id),
            executable_digest=binding.function.implementation_digest,
            backend_digest=self.status.backend_digest,
            authenticated_channel=True,
            nonce_fresh=True,
            isolation_profile=binding.route.execution_domain_profile,
            resource_reservation_id=reservation_id,
        )

    def invoke(self, request: object) -> ProviderOutcome:
        """Invoke the worker owned by the authenticated Envelope reservation."""

        if not isinstance(request, RequestEnvelope):
            raise BackendUnavailableError("Wasm request Envelope is invalid")
        reservation_id = request.resource_reservation_id
        if not isinstance(reservation_id, str) or not reservation_id:
            raise BackendUnavailableError("Wasm request reservation is missing")
        remaining = request.deadline_monotonic - time.monotonic()
        if not math.isfinite(remaining) or remaining <= 0:
            raise ProviderExecutionError("Wasm worker deadline exceeded")
        with self._lock:
            owned = self._reservations.get(reservation_id)
            if owned is None or owned.request_id is not None:
                raise BackendUnavailableError("Wasm request does not own a worker")
            if (
                request.target_domain.value != owned.domain_id
                or request.target_principal != owned.binding.principal_ref
                or request.contract_id != owned.binding.operation.contract_id
                or request.contract_version != owned.binding.operation.contract_version
                or request.operation_id != owned.binding.operation.operation_id
                or request.context.request_id in self._requests
                or request.context.target_domain_id != owned.domain_id
                or request.context.target_backend_digest != self.status.backend_digest
            ):
                raise BackendUnavailableError("Wasm request identity mismatch")
            owned.request_id = request.context.request_id
            self._requests[request.context.request_id] = reservation_id
        try:
            result = owned.worker.invoke(
                {
                    "artifact": base64.b64encode(owned.artifact).decode("ascii"),
                    "digest": owned.binding.function.implementation_digest,
                    "operation_id": request.operation_id,
                    "payload": dict(request.payload),
                },
                cancelled=request.cancellation_requested,
                timeout=min(60.0, remaining),
            )
            return ProviderOutcome(result)
        finally:
            with self._lock:
                self._requests.pop(request.context.request_id, None)

    def cancel(self, request_id: str) -> None:
        """Authenticate cancellation ownership; the shared Event stops the child."""

        with self._lock:
            reservation_id = self._requests.get(request_id)
            owned = self._reservations.get(reservation_id or "")
        if owned is None:
            raise BackendUnavailableError("cancel request does not own a Wasm worker")

    def release_materialization(self, reservation_id: str) -> None:
        """Release a reservation only after its exact worker is reaped."""

        with self._lock:
            owned = self._reservations.get(reservation_id)
        if owned is None:
            return
        owned.worker.close()
        with self._lock:
            if self._reservations.get(reservation_id) is owned:
                self._reservations.pop(reservation_id)
            if owned.request_id is not None:
                self._requests.pop(owned.request_id, None)

    def terminate(self, domain_id: str) -> None:
        """Terminate every request-scoped worker registered to one domain."""

        with self._lock:
            reservations = tuple(
                reservation_id
                for reservation_id, owned in self._reservations.items()
                if owned.domain_id == domain_id
            )
        failures: list[Exception] = []
        for reservation_id in reservations:
            try:
                self.release_materialization(reservation_id)
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise BackendUnavailableError("Wasm worker termination is unconfirmed") from failures[0]


__all__ = ["WASMTIME_PULLEY_BACKEND", "WasmComponentBackend"]
