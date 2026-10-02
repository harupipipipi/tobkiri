"""Bounded lifetime of frozen private attempt contexts and verified drain."""

from __future__ import annotations

import threading
from typing import Any, Callable, Mapping

from core_runtime.workflow_v4.attempt_store import WorkflowAttemptStoreV4
from tobkiri_host.models import OpaqueAuthorityRef, RequestContext

_TERMINAL = frozenset(
    {
        "succeeded",
        "failed",
        "ambiguous",
        "cancelled",
        "stale",
        "expired",
    }
)


def attempt_context(record: Mapping[str, Any]) -> RequestContext:
    """Reconstruct only an authenticated Host journal's frozen context."""
    document = dict(record["context"])
    document["caller_principal"] = OpaqueAuthorityRef(document["caller_principal"])
    document["delegation_chain"] = tuple(
        OpaqueAuthorityRef(value) for value in document["delegation_chain"]
    )
    return RequestContext(**document)


class WorkflowAttemptLifetimeV4:
    """Retire an exact context only after both terminal state and real drain.

    The Host callback must be idempotent and verify its exact session/domain
    identity and absence of live resources. Failures retain the durable slot;
    a later sweep retries them instead of dropping Authority ownership.
    """

    def __init__(
        self, store: WorkflowAttemptStoreV4, retire_context: Callable[[RequestContext], None]
    ) -> None:
        self._store = store
        self._retire_context = retire_context
        self._lock = threading.RLock()

    def retire_if_terminal(self, reservation_id: str) -> None:
        """Release no domain while a pending approval or child can still run."""
        with self._lock:
            revision, record = self._store.get(reservation_id)
            if (
                record.get("context_retired", False)
                or record["state"] not in _TERMINAL
                or not record.get("resources_drained", False)
            ):
                return
            if record.get("context") is not None:
                self._retire_context(attempt_context(record))
            record["context_retired"] = True
            self._store.cas(reservation_id, revision, record)

    def drained_callback(
        self, reservation_id: str, release_scope: Callable[[], None]
    ) -> Callable[[], None]:
        """Compose the genuine Broker drain with deferred Host scope release."""
        release_lock = threading.Lock()
        released = False

        def drained() -> None:
            nonlocal released
            with release_lock:
                if not released:
                    release_scope()
                    released = True
            with self._lock:
                revision, record = self._store.get(reservation_id)
                if not record.get("resources_drained", False):
                    record["resources_drained"] = True
                    self._store.cas(reservation_id, revision, record)
            self.retire_if_terminal(reservation_id)

        return drained
