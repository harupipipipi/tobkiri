"""Narrow integration ports owned by the authority and audit security core.

The host execution package intentionally does not define Grant, Lease, principal,
or execution-domain semantics. Implementations adapt these opaque DTOs to the
canonical types in ``core_runtime.authority``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol

from .contracts import ResolvedOperationBinding
from .models import OpaqueAuthorityRef, RequestContext, RuntimeEvidence
from .resources import (
    MAX_BATCH_BYTES,
    MAX_BATCH_MUTATIONS,
    OpaqueResourceHandle,
)
from .workspace_mutation import WorkspaceMutationBinding

WORKSPACE_BATCH_MAX_BYTES = MAX_BATCH_BYTES
WORKSPACE_BATCH_MAX_MUTATIONS = MAX_BATCH_MUTATIONS


@dataclass(frozen=True)
class StaticAuthorityQuery:
    """Data-only query used before queueing or materialization."""

    context: RequestContext
    target_principal: OpaqueAuthorityRef
    request_digest: str
    effect_scope: Mapping[str, Any]


@dataclass(frozen=True)
class FinalAuthorizationQuery:
    """Runtime-evidence-bound query used after materialization."""

    context: RequestContext
    target_principal: OpaqueAuthorityRef
    request_digest: str
    effect_scope: Mapping[str, Any]
    evidence: RuntimeEvidence


@dataclass(frozen=True)
class OpaqueInvocationLease:
    """Non-inspectable, request-bound lease transport DTO."""

    token: bytes

    def __post_init__(self) -> None:
        if not self.token or len(self.token) > 4096:
            raise ValueError("InvocationLease token must be non-empty and bounded")


class AuthorityPort(Protocol):
    """Adapter contract expected from ``core_runtime.authority``.

    Exact required methods:

    * ``check_static_path`` checks epoch/revocation and the existence of a
      potentially matching authority path without issuing authority.
    * ``authorize_and_issue_lease`` validates runtime evidence and returns one
      request-bound opaque lease, or raises on any unknown/mismatch/failure.
    * ``recheck_effect_boundary`` rechecks epoch, revocation, domain, and lease
      immediately before an effect boundary.
    * ``fence_request`` revokes request leases/handles after cancel or timeout.
    * ``issue_trigger_lease`` returns a trigger-occurrence-specific one-shot
      lease after checking epoch and revocation.
    """

    def check_static_path(self, query: StaticAuthorityQuery) -> None:
        """Fail closed unless a potential authority path exists."""

    def authorize_and_issue_lease(
        self,
        query: FinalAuthorizationQuery,
    ) -> OpaqueInvocationLease:
        """Return a request-bound lease after complete final authorization."""

    def recheck_effect_boundary(
        self,
        context: RequestContext,
        target: OpaqueAuthorityRef,
        lease: OpaqueInvocationLease,
    ) -> None:
        """Fail closed on epoch, revocation, domain, or lease mismatch."""

    def fence_request(self, request_id: str) -> None:
        """Revoke all request-bound authority after cancel or timeout."""

    def issue_trigger_lease(
        self,
        registration_id: str,
        occurrence_id: str,
        target: OpaqueAuthorityRef,
        security_epoch: int,
    ) -> OpaqueInvocationLease:
        """Issue one trigger-specific, one-shot lease."""


@dataclass(frozen=True)
class OpaqueAuditReservation:
    """Opaque authoritative audit reservation reference."""

    value: str


@dataclass(frozen=True)
class InteractiveApprovalRequestCommand:
    """Host-captured inputs for one explicit interactive approval decision.

    The caller and target remain opaque Host references until the authority
    adapter resolves them.  ``typed_confirmation_phrase`` is accepted only to
    derive a one-way binding; it is never returned or persisted by this port.
    """

    context: RequestContext
    target_principal: OpaqueAuthorityRef
    request_digest: str
    base_scope: Mapping[str, Any]
    invocation_owner_id: str
    presentation_owner_principal_id: str
    presentation_owner_session_id: str
    caller_publisher_lineage: str
    target_publisher_lineage: str
    expires_at: float
    redacted_metadata: Mapping[str, str]
    typed_confirmation_phrase: str | None = None


@dataclass(frozen=True)
class InteractiveApprovalDecisionCommand:
    """One locally mediated human decision; it carries no Grant material."""

    context: RequestContext
    request_id: str
    actor_id: str
    confirmation_text: str = ""
    ui_operator: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class InteractiveApprovalGetQuery:
    """An authenticated owner-scoped request for one redacted approval view."""

    context: RequestContext
    request_id: str


@dataclass(frozen=True)
class InteractiveApprovalListQuery:
    """An authenticated owner-scoped request for redacted approval views."""

    context: RequestContext
    state: str | None = None


@dataclass(frozen=True)
class InteractiveApprovalStatus:
    """Secret-free status projection returned by the narrow approval port."""

    request_id: str
    state: str
    expires_at: float
    typed_confirmation_required: bool
    request_snapshot_digest: str
    typed_confirmation_digest: str | None
    redacted_metadata: Mapping[str, str]


@dataclass(frozen=True)
class InteractiveApprovalGrantAttestation:
    """Host-only expected binding for an approved interactive one-shot Grant.

    This assertion carries no Grant, receipt, or lease material.  It lets the
    durable PendingEffect TCB prove that the completed human decision belongs
    to its exact future invocation before it is eligible for execution.
    """

    request_id: str
    context: RequestContext
    target_principal: OpaqueAuthorityRef
    request_digest: str
    base_scope: Mapping[str, Any]
    invocation_owner_id: str
    caller_publisher_lineage: str
    target_publisher_lineage: str
    expires_at: float


class InteractiveApprovalPort(Protocol):
    """Host-owned interactive approval boundary with no token return path."""

    def request_interactive_approval(
        self,
        command: InteractiveApprovalRequestCommand,
    ) -> InteractiveApprovalStatus:
        """Persist one Host-bound approval request without granting authority."""

    def approve_interactive_approval(
        self,
        command: InteractiveApprovalDecisionCommand,
    ) -> InteractiveApprovalStatus:
        """Settle a request with one atomic ApprovalRecord and one-shot Grant."""

    def deny_interactive_approval(
        self,
        command: InteractiveApprovalDecisionCommand,
    ) -> InteractiveApprovalStatus:
        """Settle a request with one denial decision and no authority material."""

    def get_interactive_approval(
        self,
        query: InteractiveApprovalGetQuery,
    ) -> InteractiveApprovalStatus:
        """Return one owner-authorized redacted approval status."""

    def list_interactive_approvals(
        self,
        query: InteractiveApprovalListQuery,
    ) -> tuple[InteractiveApprovalStatus, ...]:
        """Return owner-authorized redacted statuses, optionally by state."""

    def interactive_approval_status(self, request_id: str) -> InteractiveApprovalStatus:
        """Return the redacted lifecycle view of one approval request."""

    def assert_interactive_approval_grant(
        self,
        attestation: InteractiveApprovalGrantAttestation,
    ) -> None:
        """Fail closed unless an unused one-shot Grant matches this exact Host view."""


@dataclass(frozen=True)
class InteractiveEffectPrepareCommand:
    """Narrow request to prepare one Host-owned future effect.

    ``coordinator_principal`` is copied from the authenticated Host Provider
    envelope, never from the Pack payload.  The implementation selects every
    target identity, scope, timeout, and presentation field from the captured
    signed Profile graph.
    """

    context: RequestContext
    coordinator_principal: OpaqueAuthorityRef
    presentation_owner_principal_id: str
    presentation_owner_session_id: str
    effect_kind: str
    payload: Mapping[str, Any]
    prepared_result: Mapping[str, Any]


@dataclass(frozen=True)
class InteractiveEffectOwnerQuery:
    """Owner-scoped access to one redacted pending-effect projection."""

    context: RequestContext
    coordinator_principal: OpaqueAuthorityRef
    presentation_owner_principal_id: str
    presentation_owner_session_id: str
    effect_id: str


@dataclass(frozen=True)
class InteractiveEffectStatus:
    """Secret-free pending-effect result safe for a Pack presentation layer."""

    effect_id: str
    approval_request_id: str
    state: str
    expires_at: float
    redacted_metadata: Mapping[str, str]


class InteractiveEffectPort(Protocol):
    """Host-only coordinator port for approval-gated future effects.

    The port deliberately contains no Provider invocation, Grant, receipt,
    lease, scope, or raw prepared payload transport.  ``resume`` reaches the
    already-captured Broker only after an owner check and the durable approval
    state machine's compare-and-swap claim.
    """

    def prepare_interactive_effect(
        self,
        command: InteractiveEffectPrepareCommand,
    ) -> InteractiveEffectStatus:
        """Prepare a selected future effect and open one interactive approval."""

    def get_interactive_effect(
        self,
        query: InteractiveEffectOwnerQuery,
    ) -> InteractiveEffectStatus:
        """Return one owner-authorized redacted status."""

    def resume_interactive_effect(
        self,
        query: InteractiveEffectOwnerQuery,
    ) -> InteractiveEffectStatus:
        """Resume one approved owner-authorized future effect exactly once."""

    def cancel_interactive_effect(
        self,
        query: InteractiveEffectOwnerQuery,
    ) -> InteractiveEffectStatus:
        """Cancel one owner-authorized future effect before its dispatch edge."""


class PendingEffectPersistencePort(Protocol):
    """Encrypted Host-only persistence for a durable PendingEffect state machine."""

    def create_host_pending_effect(
        self,
        effect_id: str,
        payload: Mapping[str, Any],
    ) -> int:
        """Create an encrypted pending-effect snapshot and return revision one."""

    def get_host_pending_effect(
        self,
        effect_id: str,
    ) -> tuple[int, Mapping[str, Any]] | None:
        """Return an authenticated Host snapshot, unavailable to Packs."""

    def compare_and_swap_host_pending_effect(
        self,
        effect_id: str,
        *,
        expected_revision: int,
        payload: Mapping[str, Any],
    ) -> int:
        """Advance a Host snapshot only from one exact revision."""

    def list_host_pending_effects(self) -> list[tuple[int, Mapping[str, Any]]]:
        """Return Host snapshots for recovery without an owner index."""


class AuditPort(Protocol):
    """Fail-closed authoritative audit interface expected by the broker."""

    def reserve_effect(
        self,
        context: RequestContext,
        binding: ResolvedOperationBinding,
        request_digest: str,
    ) -> OpaqueAuditReservation:
        """Durably reserve an effect event before dispatch."""

    def mark_dispatched(self, reservation: OpaqueAuditReservation) -> None:
        """Durably record provider dispatch."""

    def commit_effect(
        self,
        reservation: OpaqueAuditReservation,
        outcome_digest: str,
    ) -> None:
        """Durably commit a completed effect."""

    def fail_effect(
        self,
        reservation: OpaqueAuditReservation,
        stable_code: str,
        ambiguous: bool,
    ) -> None:
        """Durably record failure or uncertainty without provider strings."""


@dataclass(frozen=True)
class WorkspaceMutationIdentity:
    """Broker-authenticated identity required for every workspace mutation."""

    context: RequestContext
    target_principal: OpaqueAuthorityRef
    target_domain_id: str
    target_boot_epoch: int
    target_namespace: str

    def __post_init__(self) -> None:
        if not self.target_domain_id or not self.target_namespace:
            raise ValueError("workspace mutation target binding must be non-empty")
        if self.target_boot_epoch <= 0:
            raise ValueError("workspace mutation target boot epoch must be positive")
        if self.target_domain_id != self.context.target_domain_id:
            raise ValueError("workspace mutation target domain mismatch")
        if self.target_boot_epoch != self.context.target_boot_epoch:
            raise ValueError("workspace mutation target boot epoch mismatch")
        if self.target_namespace != self.context.handle_namespace:
            raise ValueError("workspace mutation target namespace mismatch")


@dataclass(frozen=True)
class WorkspaceMutationLeaseRequest:
    """Host-captured mount binding and authenticated invocation identity."""

    identity: WorkspaceMutationIdentity
    binding: WorkspaceMutationBinding


@dataclass(frozen=True)
class OpaqueWorkspaceMutationLease:
    """Opaque lease reference which exposes no descriptor or filesystem path."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or len(self.value) > 512:
            raise ValueError("workspace mutation lease reference is invalid")


@dataclass(frozen=True)
class WorkspaceBatchMutation:
    """One opaque-handle mutation in a single Host-published batch."""

    operation: Literal["replace", "create", "delete"]
    handle: OpaqueResourceHandle
    data: bytes = b""
    mode: int = 0o600

    def __post_init__(self) -> None:
        if self.operation not in {"replace", "create", "delete"}:
            raise ValueError("workspace batch operation is invalid")
        if self.operation == "delete" and self.data:
            raise ValueError("workspace delete cannot carry content")
        if self.mode < 0 or self.mode & ~0o777:
            raise ValueError("workspace batch mode is invalid")


@dataclass(frozen=True)
class WorkspaceBatchResult:
    """Deterministic, content-free outcome of a committed Host batch."""

    transaction_id: str
    status: Literal["committed"]
    mutation_count: int
    total_bytes: int


class WorkspaceMutationPort(Protocol):
    """Narrow Host boundary for descriptor-backed workspace file mutation."""

    def acquire_lease(
        self,
        request: WorkspaceMutationLeaseRequest,
    ) -> OpaqueWorkspaceMutationLease:
        """Acquire one request- and mount-bound exclusive mutation lease."""

    def bind_existing(
        self,
        lease: OpaqueWorkspaceMutationLease,
        identity: WorkspaceMutationIdentity,
        *,
        relative_path: str,
        ttl_seconds: float,
        max_uses: int,
        max_bytes: int,
    ) -> OpaqueResourceHandle:
        """Bind an existing regular file without exposing its path or fd."""

    def bind_absent(
        self,
        lease: OpaqueWorkspaceMutationLease,
        identity: WorkspaceMutationIdentity,
        *,
        relative_path: str,
        ttl_seconds: float,
        max_uses: int,
        max_bytes: int,
    ) -> OpaqueResourceHandle:
        """Bind an absent destination for compare-and-create."""

    def replace_file(
        self,
        lease: OpaqueWorkspaceMutationLease,
        identity: WorkspaceMutationIdentity,
        handle: OpaqueResourceHandle,
        data: bytes,
    ) -> int:
        """Replace an exact existing preimage under the bound lease."""

    def create_file(
        self,
        lease: OpaqueWorkspaceMutationLease,
        identity: WorkspaceMutationIdentity,
        handle: OpaqueResourceHandle,
        data: bytes,
        *,
        mode: int = 0o600,
    ) -> int:
        """Create a file at an exact absent preimage under the bound lease."""

    def delete_file(
        self,
        lease: OpaqueWorkspaceMutationLease,
        identity: WorkspaceMutationIdentity,
        handle: OpaqueResourceHandle,
    ) -> None:
        """Delete an exact existing preimage under the bound lease."""

    def publish_batch(
        self,
        lease: OpaqueWorkspaceMutationLease,
        identity: WorkspaceMutationIdentity,
        mutations: tuple[WorkspaceBatchMutation, ...],
    ) -> WorkspaceBatchResult:
        """Publish one bounded all-or-rollback file mutation batch."""

    def close_lease(
        self,
        lease: OpaqueWorkspaceMutationLease,
        identity: WorkspaceMutationIdentity,
    ) -> None:
        """Release one exact lease and every resource handle it created."""

    def close_namespace(self, namespace: str) -> None:
        """Release all leases and handles owned by one execution namespace."""

    def close(self) -> None:
        """Release all Host workspace leases and handles during shutdown."""
