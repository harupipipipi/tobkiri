"""Single-flight lazy materialization keyed by exact workload identity."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from threading import RLock

from .backends import ExecutionBackend
from .contracts import ResolvedOperationBinding
from .models import OpaqueAuthorityRef, RuntimeEvidence


@dataclass(frozen=True)
class WorkloadInstanceKey:
    """Logical workload identity kept separate from clean VM pool identity."""

    profile_id: str
    activation_id: str
    target_principal: OpaqueAuthorityRef
    execution_domain_profile: str
    security_epoch: int


class MaterializationCoordinator:
    """Deduplicate concurrent cold starts without merging authority principals."""

    def __init__(self) -> None:
        self._starting: dict[tuple[WorkloadInstanceKey, str], Future[RuntimeEvidence]] = {}
        self._lock = RLock()

    def materialize(
        self,
        key: WorkloadInstanceKey,
        backend: ExecutionBackend,
        binding: ResolvedOperationBinding,
        reservation_id: str,
    ) -> RuntimeEvidence:
        """Start once per exact concurrent workload and reservation.

        Resident-domain reuse and its continuing resource charge belong to the
        backend resource controller. This coordinator never keeps an uncharged
        ready-domain cache.
        """
        # Runtime evidence belongs to the reservation that funded its domain.
        # Sharing it with another ticket breaks attestation and lets either
        # caller release resources still in use by the other caller.
        start_key = (key, reservation_id)
        with self._lock:
            future = self._starting.get(start_key)
            owner = future is None
            if future is None:
                future = Future()
                self._starting[start_key] = future
        if not owner:
            return future.result()
        try:
            evidence = backend.materialize(binding, reservation_id)
            future.set_result(evidence)
            return evidence
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._starting.pop(start_key, None)
