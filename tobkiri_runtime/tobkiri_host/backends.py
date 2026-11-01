"""Execution selection and all-or-nothing production backend feature gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterable, Protocol, runtime_checkable

from .contracts import ResolvedOperationBinding
from .errors import BackendUnavailableError
from .models import ExecutionKind, RuntimeEvidence

if TYPE_CHECKING:
    from .artifact_materialization import MaterializedPackArtifact
    from .platform_backends import PlatformIsolationDriver


REQUIRED_PRODUCTION_GATES = frozenset(
    {
        "artifact_verified",
        "static_admission",
        "security_epoch",
        "authority",
        "audit",
        "resource_controller",
        "runtime_evidence",
        "cancellation",
    }
)


@dataclass(frozen=True)
class BackendStatus:
    """Backend availability with independently verifiable safety gates."""

    backend_id: str
    execution_kind: ExecutionKind
    platform: str
    backend_digest: str
    production_enabled: bool = False
    conformance_only: bool = True
    satisfied_gates: frozenset[str] = frozenset()
    unavailable_reason: str | None = None
    enforces_platform: bool = False
    requires_platform_attestation: bool = False

    @property
    def ready_for_production(self) -> bool:
        """Return true only when the backend and every prerequisite are ready."""
        return (
            self.production_enabled
            and not self.conformance_only
            and REQUIRED_PRODUCTION_GATES <= self.satisfied_gates
        )


class ExecutionBackend(Protocol):
    """Common materialization and invocation supervisor interface."""

    status: BackendStatus

    def materialize(
        self,
        binding: ResolvedOperationBinding,
        reservation_id: str,
    ) -> RuntimeEvidence:
        """Start or reuse an exact workload and return Host-verified evidence."""

    def invoke(self, request: object) -> object:
        """Invoke through the authenticated backend channel."""

    def cancel(self, request_id: str) -> None:
        """Fence new I/O and terminate Host-owned local execution."""

    def terminate(self, domain_id: str) -> None:
        """Destroy a mismatched or revoked execution domain."""


@runtime_checkable
class RequestScopedBackend(ExecutionBackend, Protocol):
    """Backend whose workers are paid for by one Broker request reservation.

    The memory floor is Host-owned and covers compilation and guest execution;
    a lower Pack declaration must not reduce the reserved amount.
    """

    memory_reservation_bytes: int

    def release_materialization(self, reservation_id: str) -> None:
        """Confirm all workers for this reservation have exited before returning.

        This also applies to partial starts and final-authorization denials.
        Raise if termination cannot be confirmed; the Broker retains the charge.
        Resident backends instead own continuing resource accounting separately.
        """


class BackendRegistry:
    """Select exact backends without a weaker fallback."""

    def __init__(self, backends: Iterable[ExecutionBackend]) -> None:
        items = tuple(backends)
        grouped: dict[str, list[ExecutionBackend]] = {}
        for backend in items:
            grouped.setdefault(backend.status.backend_id, []).append(backend)
        self._backends = {
            backend_id: tuple(candidates)
            for backend_id, candidates in grouped.items()
        }
        self._registered = items

    def select(
        self,
        binding: ResolvedOperationBinding,
        *,
        production: bool = True,
    ) -> ExecutionBackend:
        """Select the variant-pinned backend and enforce its feature gates."""
        candidates = self._backends.get(binding.variant.backend, ())
        matching = tuple(
            backend
            for backend in candidates
            if not callable(getattr(backend, "supports", None))
            or bool(backend.supports(binding))  # type: ignore[attr-defined]
        )
        if not matching:
            raise BackendUnavailableError("pinned backend is not installed")
        if len(matching) != 1:
            raise BackendUnavailableError("pinned backend contribution is ambiguous")
        backend = matching[0]
        status = backend.status
        if status.enforces_platform:
            requested_platform = (
                None
                if binding.variant.os == "any" and binding.variant.architecture == "any"
                else f"{binding.variant.os}-{binding.variant.architecture}"
            )
            if requested_platform is not None and status.platform != requested_platform:
                raise BackendUnavailableError("backend platform does not match pinned variant")
        if status.execution_kind is not binding.variant.execution_kind:
            raise BackendUnavailableError("backend execution kind mismatch")
        if production and not status.ready_for_production:
            if status.unavailable_reason is not None:
                raise BackendUnavailableError(status.unavailable_reason)
            missing = sorted(REQUIRED_PRODUCTION_GATES - status.satisfied_gates)
            raise BackendUnavailableError(f"backend is feature-disabled; missing gates: {missing}")
        if not production and not (status.conformance_only or status.ready_for_production):
            raise BackendUnavailableError("backend is disabled")
        return backend

    @property
    def statuses(self) -> tuple[BackendStatus, ...]:
        """Return registered backend status in deterministic ID order."""
        return tuple(
            backend.status
            for backend in sorted(
                self._registered,
                key=lambda item: (item.status.backend_id, item.status.backend_digest),
            )
        )

    @property
    def registered(self) -> tuple[ExecutionBackend, ...]:
        """Return exact registered backends for composition-root extension."""

        return tuple(
            sorted(
                self._registered,
                key=lambda item: (item.status.backend_id, item.status.backend_digest),
            )
        )


def production_backend_registry(
    *,
    platform_system: str | None = None,
    machine: str | None = None,
    drivers: Iterable[PlatformIsolationDriver] = (),
    artifact_resolver: Callable[
        [ResolvedOperationBinding], MaterializedPackArtifact
    ]
    | None = None,
) -> BackendRegistry:
    """Register the one documented PackVM substrate for the current platform.

    A caller may inject an already-authenticated platform driver.  Host probing
    never launches helpers, reads environment variables, or substitutes another
    substrate.  An unavailable dependency is represented by a disabled backend,
    so selecting it returns the stable ``backend_unavailable`` result.
    """
    from .platform_backends import build_platform_backend

    backend = build_platform_backend(
        platform_system=platform_system,
        machine=machine,
        drivers=drivers,
        artifact_resolver=artifact_resolver,
    )
    return BackendRegistry((backend,))


__all__ = [
    "BackendRegistry",
    "BackendStatus",
    "ExecutionBackend",
    "RequestScopedBackend",
    "REQUIRED_PRODUCTION_GATES",
    "production_backend_registry",
]
