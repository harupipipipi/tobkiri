"""Canonical data models for Workflow v4."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest


class WorkflowError(RuntimeError):
    """Base class for Workflow v4 failures."""


class WorkflowValidationError(WorkflowError):
    """Raised when untrusted workflow input is invalid."""


class WorkflowConflict(WorkflowError):
    """Raised when an ETag, revision, or replay fence conflicts."""


class WorkflowDenied(WorkflowError):
    """Raised when authority fails closed."""


class WorkflowNotFound(WorkflowError):
    """Raised when a Workflow v4 record is unavailable."""


class _StrEnum(str, Enum):
    """Backport the string formatting behavior of enum.StrEnum."""

    __str__ = str.__str__


class DefinitionState(_StrEnum):
    """Definition lifecycle states."""

    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class RunState(_StrEnum):
    """Workflow run lifecycle states."""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    NEEDS_RECONCILIATION = "needs_reconciliation"


class StepAttemptState(_StrEnum):
    """One immutable attempt's state."""

    PENDING = "pending"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    AMBIGUOUS_EFFECT = "ambiguous_effect"


class ApprovalState(_StrEnum):
    """States returned by the injected Authority provider."""

    RESERVED = "reserved"
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    REVOKED = "revoked"


TERMINAL_RUN_STATES = frozenset(
    {
        RunState.SUCCEEDED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.TIMED_OUT,
        RunState.NEEDS_RECONCILIATION,
    }
)
TERMINAL_ATTEMPT_STATES = frozenset(
    {
        StepAttemptState.SUCCEEDED,
        StepAttemptState.FAILED,
        StepAttemptState.CANCELLED,
        StepAttemptState.TIMED_OUT,
        StepAttemptState.AMBIGUOUS_EFFECT,
    }
)

RUN_TRANSITIONS = {
    RunState.QUEUED: frozenset({RunState.RUNNING, RunState.CANCELLED}),
    RunState.RUNNING: frozenset(
        {
            RunState.PAUSED,
            RunState.WAITING_APPROVAL,
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.TIMED_OUT,
            RunState.NEEDS_RECONCILIATION,
        }
    ),
    RunState.PAUSED: frozenset({RunState.RUNNING, RunState.CANCELLED}),
    RunState.WAITING_APPROVAL: frozenset(
        {RunState.RUNNING, RunState.FAILED, RunState.CANCELLED, RunState.TIMED_OUT}
    ),
}

ATTEMPT_TRANSITIONS = {
    StepAttemptState.PENDING: frozenset(
        {
            StepAttemptState.DISPATCHING,
            StepAttemptState.WAITING_APPROVAL,
            StepAttemptState.SUCCEEDED,
            StepAttemptState.FAILED,
            StepAttemptState.CANCELLED,
        }
    ),
    StepAttemptState.DISPATCHING: frozenset(
        {
            StepAttemptState.RUNNING,
            StepAttemptState.CANCELLED,
            StepAttemptState.AMBIGUOUS_EFFECT,
        }
    ),
    StepAttemptState.RUNNING: frozenset(
        {
            StepAttemptState.SUCCEEDED,
            StepAttemptState.FAILED,
            StepAttemptState.CANCELLED,
            StepAttemptState.TIMED_OUT,
            StepAttemptState.AMBIGUOUS_EFFECT,
        }
    ),
    StepAttemptState.WAITING_APPROVAL: frozenset(
        {
            StepAttemptState.PENDING,
            StepAttemptState.FAILED,
            StepAttemptState.CANCELLED,
            StepAttemptState.TIMED_OUT,
        }
    ),
}


def digest(value: Any) -> str:
    """Return the repository's canonical SHA-256 digest."""

    return canonical_digest(value)


def etag(definition_id: str, revision: int, revision_digest: str) -> str:
    """Build a strong opaque ETag for optimistic locking."""

    return f'"{digest({"definition_id": definition_id, "revision": revision, "digest": revision_digest})}"'


@dataclass(frozen=True, slots=True)
class OperationBinding:
    """One exact operation exposed by the active Contract catalog."""

    contract_id: str
    contract_revision_digest: str
    operation_id: str
    function_principal_id: str
    provider_id: str
    input_schema_digest: str
    effect_ceiling: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str, str, str]:
        """Return the exact non-ambient operation identity."""

        return (
            self.contract_id,
            self.contract_revision_digest,
            self.operation_id,
            self.function_principal_id,
        )


@dataclass(frozen=True, slots=True)
class AuthorityReservation:
    """Opaque authority reservation safe to reference from a checkpoint."""

    reservation_id: str
    state: ApprovalState
    request_digest: str
    security_epoch: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class DispatchAuthority:
    """Ephemeral one-dispatch authority; never persisted in workflow state."""

    dispatch_token: str
    reservation_id: str
    request_digest: str
    security_epoch: int


@dataclass(frozen=True, slots=True)
class InvocationOutcome:
    """Provider outcome with explicit external-effect ambiguity."""

    output: Mapping[str, Any] | None = None
    error_code: str | None = None
    ambiguous_effect: bool = False
    timed_out: bool = False


def require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    """Reject non-object inputs before they reach persistence or providers."""

    if not isinstance(value, Mapping):
        raise WorkflowValidationError(f"{name} must be an object")
    return value
