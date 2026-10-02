"""Typed private Host port for captured Workflow attempt execution.

This port supplies no authority by itself. Production binds it only after the
verified factory and the signed outgoing Function edges have been captured.
Neither authenticated invocation objects nor dispatch tokens are wire payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, ContextManager, Literal, Mapping, Protocol

from core_runtime.authority.v4 import AuthorityScope
from core_runtime.workflow_v4.models import (
    AuthorityReservation,
    DispatchAuthority,
    InvocationOutcome,
    WorkflowDenied,
)
from tobkiri_host.broker import RequestBroker
from tobkiri_host.contracts import ResolvedOperationBinding
from tobkiri_host.models import OpaqueAuthorityRef, RequestContext
from tobkiri_host.ports import AuthorityPort, InteractiveApprovalPort
from tobkiri_protocol.canonical import canonical_digest

if TYPE_CHECKING:
    from core_runtime.host_provider_backend_v4 import HostProviderInvocationContextV4


@dataclass(frozen=True)
class WorkflowAttemptDeclarationV4:
    """Finite verified factory request; it does not select dispatch targets."""

    function_id: str = "tobkiri.workflow.provider"
    contract_id: str = "tobkiri.workflow.v4"
    operation_ids: tuple[str, ...] = (
        "run.advance",
        "run.cancel",
        "run.step.execute",
        "run.step.resume",
        "run.step.retry",
    )
    api_version: str = "io.tobkiri.workflow-attempt-port.v4"

    def __post_init__(self) -> None:
        if (
            self.function_id != "tobkiri.workflow.provider"
            or self.contract_id != "tobkiri.workflow.v4"
            or self.api_version != "io.tobkiri.workflow-attempt-port.v4"
            or self.operation_ids
            != (
                "run.advance",
                "run.cancel",
                "run.step.execute",
                "run.step.resume",
                "run.step.retry",
            )
        ):
            raise WorkflowDenied("Workflow attempt declaration is invalid")

    @property
    def digest(self) -> str:
        """Return the canonical digest of the complete finite declaration."""
        return canonical_digest(
            {
                "api_version": self.api_version,
                "function_id": self.function_id,
                "contract_id": self.contract_id,
                "operation_ids": list(self.operation_ids),
            }
        )


@dataclass(frozen=True)
class CapturedWorkflowAttemptRouteV4:
    """One signed outgoing caller/target binding and its unchanged ceiling."""

    caller_principal: OpaqueAuthorityRef
    binding: ResolvedOperationBinding
    caller_effect_ceiling: AuthorityScope
    authority_mode: Literal["profile_grant", "interactive_only"]

    def __post_init__(self) -> None:
        if self.authority_mode not in {"profile_grant", "interactive_only"}:
            raise WorkflowDenied("Workflow attempt authority mode is invalid")

    @property
    def key(self) -> tuple[str, str, str, str]:
        """Return the exact target identity used by a compiled Workflow step."""
        operation = self.binding.operation
        return (
            operation.contract_id,
            operation.revision_digest,
            operation.operation_id,
            self.binding.principal_ref.value,
        )


class WorkflowAttemptPortV4(Protocol):
    """Host-only durable reservation and normal Broker execution interface."""

    def reserve(
        self,
        invocation: HostProviderInvocationContextV4,
        run: Mapping[str, Any],
        attempt: Mapping[str, Any],
    ) -> AuthorityReservation:
        """Prepare the sealed attempt and obtain ordinary Host approval."""

    def inspect(
        self, invocation: HostProviderInvocationContextV4, reservation_id: str
    ) -> AuthorityReservation:
        """Revalidate owner/capture and the same durable approval."""

    def commit(
        self,
        invocation: HostProviderInvocationContextV4,
        reservation_id: str,
        *,
        request_digest: str,
        security_epoch: int,
    ) -> DispatchAuthority:
        """CAS-claim one dispatch capability for this fresh live invocation."""

    def finish(
        self,
        invocation: HostProviderInvocationContextV4,
        reservation_id: str,
        *,
        outcome_digest: str,
        state: str,
    ) -> None:
        """Seal a result or conservative ambiguity without redispatch."""

    def revoke(
        self, invocation: HostProviderInvocationContextV4, reservation_id: str, *, reason: str
    ) -> None:
        """Fence the exact owned pending attempt."""

    def invoke(
        self,
        invocation: HostProviderInvocationContextV4,
        request: Mapping[str, Any],
        *,
        authority: DispatchAuthority,
    ) -> InvocationOutcome:
        """Dispatch the frozen snapshot through the ordinary Broker."""

    def cancel(self, invocation: HostProviderInvocationContextV4, request_id: str) -> None:
        """Cancel only this owner's exact active child request."""


class LateBoundWorkflowAttemptPortV4:
    """Single-binding port created before production's Broker exists."""

    def __init__(self) -> None:
        self._service: WorkflowAttemptPortV4 | None = None
        self._lock = threading.Lock()

    def bind(self, service: WorkflowAttemptPortV4) -> None:
        """Bind exactly one Host service; replacement is prohibited."""
        with self._lock:
            if self._service is not None:
                raise WorkflowDenied("Workflow attempt port is already bound")
            self._service = service

    def _current(self) -> WorkflowAttemptPortV4:
        with self._lock:
            service = self._service
        if service is None:
            raise WorkflowDenied("Workflow attempt execution is unavailable")
        return service

    def reserve(
        self,
        invocation: HostProviderInvocationContextV4,
        run: Mapping[str, Any],
        attempt: Mapping[str, Any],
    ) -> AuthorityReservation:
        """Forward only after a Host service has been bound."""
        return self._current().reserve(invocation, run, attempt)

    def inspect(
        self, invocation: HostProviderInvocationContextV4, reservation_id: str
    ) -> AuthorityReservation:
        """Inspect an owned durable reservation."""
        return self._current().inspect(invocation, reservation_id)

    def commit(
        self,
        invocation: HostProviderInvocationContextV4,
        reservation_id: str,
        *,
        request_digest: str,
        security_epoch: int,
    ) -> DispatchAuthority:
        """Claim through the bound service's durable CAS."""
        return self._current().commit(
            invocation, reservation_id, request_digest=request_digest, security_epoch=security_epoch
        )

    def finish(
        self,
        invocation: HostProviderInvocationContextV4,
        reservation_id: str,
        *,
        outcome_digest: str,
        state: str,
    ) -> None:
        """Finish through the bound service."""
        self._current().finish(
            invocation, reservation_id, outcome_digest=outcome_digest, state=state
        )

    def revoke(
        self, invocation: HostProviderInvocationContextV4, reservation_id: str, *, reason: str
    ) -> None:
        """Revoke through the bound service."""
        self._current().revoke(invocation, reservation_id, reason=reason)

    def invoke(
        self,
        invocation: HostProviderInvocationContextV4,
        request: Mapping[str, Any],
        *,
        authority: DispatchAuthority,
    ) -> InvocationOutcome:
        """Invoke through the bound service; no callback shortcut exists."""
        return self._current().invoke(invocation, request, authority=authority)

    def cancel(self, invocation: HostProviderInvocationContextV4, request_id: str) -> None:
        """Cancel through the bound service."""
        self._current().cancel(invocation, request_id)


@dataclass(frozen=True)
class WorkflowAttemptServiceConfigV4:
    """Host assembly inputs, never a Pack/client-controlled configuration."""

    broker: RequestBroker
    authority: AuthorityPort
    approvals: InteractiveApprovalPort
    routes: tuple[CapturedWorkflowAttemptRouteV4, ...]
    context_for_attempt: Callable[
        [CapturedWorkflowAttemptRouteV4, HostProviderInvocationContextV4, str],
        RequestContext,
    ]
    execution_scope: Callable[
        [RequestContext, HostProviderInvocationContextV4],
        ContextManager[Callable[[], None]],
    ]
    retire_context: Callable[[RequestContext], None]
    presentation_owner_scope: Callable[[RequestContext, str, str], ContextManager[None]]
    assert_current_capture: Callable[[], None]
    state_path: Path
    coordinator_bindings: tuple[ResolvedOperationBinding, ...]
    coordinator_publisher_lineage: str
    profile_id: str
    activation_id: str
    activation_digest: str
    plan_digest: str
    security_epoch: int
    max_active_attempts: int = 64
    clock: Callable[[], float] = time.time
    monotonic_clock: Callable[[], float] = time.monotonic


def create_workflow_attempt_service_v4(
    config: WorkflowAttemptServiceConfigV4,
) -> WorkflowAttemptPortV4:
    """Assemble the private service only after capture has built its Broker."""
    from core_runtime.workflow_v4.attempt_service import HostWorkflowAttemptServiceV4

    return HostWorkflowAttemptServiceV4(config)
