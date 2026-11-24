"""Bind the generic Workflow engine to one fresh authenticated Host invocation."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from core_runtime.host_provider_backend_v4 import HostProviderInvocationContextV4
from core_runtime.workflow_v4.attempt_binding import stored_attempt_for_query
from core_runtime.workflow_v4.attempt_port import WorkflowAttemptPortV4
from core_runtime.workflow_v4.models import (
    AuthorityReservation,
    DispatchAuthority,
    InvocationOutcome,
    WorkflowDenied,
    WorkflowCancellationUnconfirmed,
)
from core_runtime.workflow_v4.store import WorkflowStoreV4


class CapturedWorkflowAttemptAdapterV4:
    """Keep invocation authority ephemeral while Workflow records remain durable."""

    def __init__(
        self,
        *,
        store: WorkflowStoreV4,
        port: WorkflowAttemptPortV4 | None,
        invocation: HostProviderInvocationContextV4,
    ) -> None:
        self._store = store
        self._port = port
        self._invocation = invocation

    def _current(self) -> WorkflowAttemptPortV4:
        self._invocation.assert_current()
        if self._port is None:
            raise WorkflowDenied("Workflow attempt execution is unavailable")
        return self._port

    def reserve(self, request: Mapping[str, Any]) -> AuthorityReservation:
        """Resolve all input from the sealed store, not the query projection."""
        port = self._current()
        run, attempt = stored_attempt_for_query(self._store, request)
        return port.reserve(self._invocation, run, attempt)

    def inspect(self, reservation_id: str) -> AuthorityReservation:
        """Use the current invocation to revalidate an existing reservation."""
        return self._current().inspect(self._invocation, reservation_id)

    def commit(
        self, reservation_id: str, *, request_digest: str, security_epoch: int
    ) -> DispatchAuthority:
        """Commit through the captured private port, never client approval."""
        return self._current().commit(
            self._invocation,
            reservation_id,
            request_digest=request_digest,
            security_epoch=security_epoch,
        )

    def finish(self, reservation_id: str, *, outcome_digest: str, state: str) -> None:
        """Record terminal evidence through the same owner-bound Host port."""
        self._current().finish(
            self._invocation, reservation_id, outcome_digest=outcome_digest, state=state
        )

    def revoke(self, reservation_id: str, *, reason: str) -> None:
        """Fence this exact owned reservation."""
        self._current().revoke(self._invocation, reservation_id, reason=reason)

    def invoke(
        self, request: Mapping[str, Any], *, authority: DispatchAuthority,
        dispatch_fence: Callable[[str], None] | None = None,
    ) -> InvocationOutcome:
        """Invoke the immutable prepared request through the real Broker."""
        return self._current().invoke(
            self._invocation, request, authority=authority, dispatch_fence=dispatch_fence
        )

    def cancel(self, request_id: str) -> None:
        """Propagate authenticated cancellation to the exact owned request."""
        self._invocation.assert_current()
        if self._port is None:
            raise WorkflowCancellationUnconfirmed("Workflow cancellation proof is unavailable")
        self._port.cancel(self._invocation, request_id)
