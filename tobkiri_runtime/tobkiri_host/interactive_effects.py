"""Host-owned durable state for approval-gated future effects.

This module is deliberately a TCB primitive, not a Pack port.  It retains the
full future request and normalized provider payload only in AuthorityStore's
encrypted pending-effect table.  Its public results are redacted state views;
no method returns a provider payload, UI token, Grant, receipt, or lease.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping

from core_runtime.authority.v4 import AuthorityScope, authority_digest
from core_runtime.authority.v4_models import canonical_json

from .broker import PreparedInvocation, PreparedInvocationSnapshot, RequestBroker
from .models import OpaqueAuthorityRef, RequestContext
from .ports import (
    InteractiveApprovalGrantAttestation,
    InteractiveApprovalPort,
    InteractiveEffectOwnerQuery,
    InteractiveEffectPort,
    InteractiveEffectPrepareCommand,
    InteractiveEffectStatus,
    PendingEffectPersistencePort,
)


class PendingEffectError(RuntimeError):
    """Fail-closed public error for unavailable PendingEffect state."""


class PendingEffectState(str, Enum):
    """Durable lifecycle of a Host-owned approval-gated effect."""

    PREPARED = "prepared"
    APPROVAL_PENDING = "approval_pending"
    APPROVED = "approved"
    CLAIMED = "claimed"
    DISPATCHED = "dispatched"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALE = "stale"
    AMBIGUOUS = "ambiguous"
    CANCELLED = "cancelled"


_TERMINAL_STATES = frozenset(
    {
        PendingEffectState.SUCCEEDED,
        PendingEffectState.FAILED,
        PendingEffectState.STALE,
        PendingEffectState.AMBIGUOUS,
        PendingEffectState.CANCELLED,
    }
)


@dataclass(frozen=True)
class PendingEffectStatus:
    """Redacted state projection; safe for a future Host/UI boundary."""

    effect_id: str
    approval_request_id: str
    state: PendingEffectState
    revision: int
    expires_at: float
    presentation_metadata: Mapping[str, str]


@dataclass(frozen=True)
class _PendingEffect:
    """Full encrypted Host-only PendingEffect record."""

    effect_id: str
    approval_request_id: str
    state: PendingEffectState
    context: RequestContext
    prepared: PreparedInvocationSnapshot
    effect_scope: Mapping[str, Any]
    invocation_owner_id: str
    presentation_owner_principal_id: str
    presentation_owner_session_id: str
    presentation_metadata: Mapping[str, str]
    expires_at: float
    created_at: float
    updated_at: float
    outcome_digest: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.effect_id, str)
            or not self.effect_id
            or not isinstance(self.approval_request_id, str)
            or not self.approval_request_id
            or not isinstance(self.invocation_owner_id, str)
            or not self.invocation_owner_id
            or not isinstance(self.presentation_owner_principal_id, str)
            or not self.presentation_owner_principal_id
            or not isinstance(self.presentation_owner_session_id, str)
            or not self.presentation_owner_session_id
            or self.expires_at <= self.created_at
            or self.updated_at < self.created_at
            or self.prepared.request_digest
            != AuthorityScope.from_dict(self.effect_scope).exact_request_digest
        ):
            raise PendingEffectError("pending effect is unavailable")
        if (
            self.context.caller_principal.value
            == _target_principal(self.prepared).value
        ):
            raise PendingEffectError("pending effect is unavailable")
        object.__setattr__(self, "effect_scope", _frozen_json(self.effect_scope))
        object.__setattr__(
            self,
            "presentation_metadata",
            _redacted_metadata(self.presentation_metadata),
        )

    def with_state(
        self,
        state: PendingEffectState,
        *,
        now: float,
        outcome_digest: str | None = None,
    ) -> "_PendingEffect":
        """Return one immutable state transition ready for persistence CAS."""

        return replace(
            self,
            state=state,
            updated_at=now,
            outcome_digest=outcome_digest,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the complete encrypted-at-rest Host snapshot."""

        return {
            "effect_id": self.effect_id,
            "approval_request_id": self.approval_request_id,
            "state": self.state.value,
            "context": _context_to_dict(self.context),
            "prepared": self.prepared.to_dict(),
            "effect_scope": _thaw_json(self.effect_scope),
            "invocation_owner_id": self.invocation_owner_id,
            "presentation_owner_principal_id": self.presentation_owner_principal_id,
            "presentation_owner_session_id": self.presentation_owner_session_id,
            "presentation_metadata": dict(self.presentation_metadata),
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "outcome_digest": self.outcome_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "_PendingEffect":
        """Load and validate one decrypted Host snapshot."""

        try:
            state = PendingEffectState(str(value["state"]))
            metadata = value.get("presentation_metadata", {})
            scope = value["effect_scope"]
            if not isinstance(metadata, Mapping) or not isinstance(scope, Mapping):
                raise TypeError("invalid pending effect shape")
            return cls(
                effect_id=str(value["effect_id"]),
                approval_request_id=str(value["approval_request_id"]),
                state=state,
                context=_context_from_dict(value["context"]),
                prepared=PreparedInvocationSnapshot.from_dict(value["prepared"]),
                effect_scope=dict(scope),
                invocation_owner_id=str(value["invocation_owner_id"]),
                presentation_owner_principal_id=str(
                    value["presentation_owner_principal_id"]
                ),
                presentation_owner_session_id=str(
                    value["presentation_owner_session_id"]
                ),
                presentation_metadata={
                    str(key): str(item) for key, item in metadata.items()
                },
                expires_at=float(value["expires_at"]),
                created_at=float(value["created_at"]),
                updated_at=float(value["updated_at"]),
                outcome_digest=(
                    str(value["outcome_digest"])
                    if value.get("outcome_digest") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError, PendingEffectError) as exc:
            raise PendingEffectError("pending effect is unavailable") from exc


class PendingEffectController:
    """Host TCB state machine for a durable, approval-gated future effect."""

    def __init__(
        self,
        *,
        persistence: PendingEffectPersistencePort,
        approvals: InteractiveApprovalPort,
        coordinator_principal: OpaqueAuthorityRef,
        coordinator_publisher_lineage: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._persistence = persistence
        self._approvals = approvals
        self._coordinator_principal = coordinator_principal
        if not coordinator_publisher_lineage:
            raise PendingEffectError("pending effect is unavailable")
        self._coordinator_publisher_lineage = coordinator_publisher_lineage
        self._clock = clock

    def prepare(
        self,
        *,
        prepared: PreparedInvocation,
        context: RequestContext,
        effect_scope: Mapping[str, Any],
        invocation_owner_id: str,
        presentation_owner_principal_id: str,
        presentation_owner_session_id: str,
        presentation_metadata: Mapping[str, str],
        expires_at: float,
        typed_confirmation_phrase: str | None = None,
    ) -> PendingEffectStatus:
        """Persist and open one Host-bound interactive approval request.

        The raw normalized payload is written before the approval request but
        never leaves this controller.  A crash between those two transactions
        leaves ``PREPARED``; recovery either observes the durable request and
        advances it, or terminally marks the record stale.
        """

        now = self._clock()
        try:
            snapshot = prepared.to_snapshot()
            scope = AuthorityScope.from_dict(effect_scope)
        except Exception as exc:
            raise PendingEffectError("pending effect is unavailable") from exc
        if (
            context.caller_principal != self._coordinator_principal
            or context.delegation_chain
            or scope.exact_request_digest != snapshot.request_digest
            or scope.dimensions.get("invocation_owner_id") != (invocation_owner_id,)
            or scope.dimensions.get("caller_session_id") != (context.caller_session_id,)
            or scope.dimensions.get("plan_digest") != (context.plan_digest,)
            or (
                typed_confirmation_phrase is not None
                and presentation_metadata.get("confirmation_phrase")
                != typed_confirmation_phrase
            )
            or expires_at <= now
        ):
            raise PendingEffectError("pending effect is unavailable")
        effect_id, approval_request_id = _new_effect_identifiers()
        record = _PendingEffect(
            effect_id=effect_id,
            approval_request_id=approval_request_id,
            state=PendingEffectState.PREPARED,
            context=context,
            prepared=snapshot,
            effect_scope=scope.to_dict(),
            invocation_owner_id=invocation_owner_id,
            presentation_owner_principal_id=presentation_owner_principal_id,
            presentation_owner_session_id=presentation_owner_session_id,
            presentation_metadata=presentation_metadata,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        try:
            revision = self._persistence.create_host_pending_effect(
                effect_id,
                record.to_dict(),
            )
        except Exception as exc:
            raise PendingEffectError("pending effect is unavailable") from exc
        approval_context = replace(context, request_id=approval_request_id)
        try:
            status = self._approvals.request_interactive_approval(
                _approval_request_command(
                    record,
                    approval_context=approval_context,
                    coordinator_publisher_lineage=self._coordinator_publisher_lineage,
                    typed_confirmation_phrase=typed_confirmation_phrase,
                )
            )
            if status.request_id != approval_request_id or status.state != "pending":
                raise PendingEffectError("pending effect is unavailable")
        except Exception as exc:
            self._best_effort_transition(
                effect_id,
                revision,
                record,
                PendingEffectState.STALE,
            )
            if isinstance(exc, PendingEffectError):
                raise
            raise PendingEffectError("pending effect is unavailable") from exc
        return self._transition(
            effect_id,
            revision,
            record,
            PendingEffectState.APPROVAL_PENDING,
        )

    def status(self, effect_id: str) -> PendingEffectStatus:
        """Return a redacted durable status without payload or authority material."""

        revision, record = self._load(effect_id)
        return _status(record, revision)

    def status_for_presentation(
        self,
        *,
        effect_id: str,
        presentation_owner_principal_id: str,
        presentation_owner_session_id: str,
    ) -> PendingEffectStatus:
        """Return status only to the durable presentation owner.

        An effect identifier is deliberately not an authorization capability.
        This check happens before observing approval state, so a foreign
        caller cannot use the state machine as an oracle either.
        """

        _revision, record = self._load_owned(
            effect_id,
            presentation_owner_principal_id=presentation_owner_principal_id,
            presentation_owner_session_id=presentation_owner_session_id,
        )
        return self.observe_approval(record.effect_id)

    def resume_for_presentation(
        self,
        *,
        effect_id: str,
        presentation_owner_principal_id: str,
        presentation_owner_session_id: str,
        broker: RequestBroker,
    ) -> PendingEffectStatus:
        """Resume only when the authenticated presentation owner still matches."""

        self._load_owned(
            effect_id,
            presentation_owner_principal_id=presentation_owner_principal_id,
            presentation_owner_session_id=presentation_owner_session_id,
        )
        observed = self.observe_approval(effect_id)
        if observed.state is not PendingEffectState.APPROVED:
            return observed
        return self.resume(effect_id, broker)

    def cancel_for_presentation(
        self,
        *,
        effect_id: str,
        presentation_owner_principal_id: str,
        presentation_owner_session_id: str,
    ) -> PendingEffectStatus:
        """Cancel only when the authenticated presentation owner still matches."""

        self._load_owned(
            effect_id,
            presentation_owner_principal_id=presentation_owner_principal_id,
            presentation_owner_session_id=presentation_owner_session_id,
        )
        return self.cancel(effect_id)

    def observe_approval(self, effect_id: str) -> PendingEffectStatus:
        """Synchronize a durable effect with its Host-owned approval record."""

        revision, record = self._load(effect_id)
        if (
            record.state in _TERMINAL_STATES
            or record.state is PendingEffectState.APPROVED
        ):
            return _status(record, revision)
        if record.state is PendingEffectState.PREPARED:
            return self._recover_prepared(effect_id, revision, record)
        if record.state is not PendingEffectState.APPROVAL_PENDING:
            return _status(record, revision)
        try:
            approval = self._approvals.interactive_approval_status(
                record.approval_request_id
            )
        except Exception:
            return self._transition(
                effect_id, revision, record, PendingEffectState.STALE
            )
        if approval.state == "pending" and approval.expires_at > self._clock():
            return _status(record, revision)
        if approval.state == "approved":
            try:
                self._approvals.assert_interactive_approval_grant(
                    _grant_attestation(
                        record,
                        coordinator_publisher_lineage=self._coordinator_publisher_lineage,
                    )
                )
            except Exception:
                return self._transition(
                    effect_id, revision, record, PendingEffectState.STALE
                )
            return self._transition(
                effect_id, revision, record, PendingEffectState.APPROVED
            )
        return self._transition(
            effect_id, revision, record, PendingEffectState.CANCELLED
        )

    def claim(self, effect_id: str) -> PendingEffectStatus:
        """CAS-claim an approved effect for a future Host execution resumer."""

        observed = self.observe_approval(effect_id)
        if observed.state is not PendingEffectState.APPROVED:
            raise PendingEffectError("pending effect is unavailable")
        revision, record = self._load(effect_id)
        if record.state is not PendingEffectState.APPROVED:
            raise PendingEffectError("pending effect is unavailable")
        try:
            self._approvals.assert_interactive_approval_grant(
                _grant_attestation(
                    record,
                    coordinator_publisher_lineage=self._coordinator_publisher_lineage,
                )
            )
        except Exception as exc:
            self._best_effort_transition(
                effect_id,
                revision,
                record,
                PendingEffectState.STALE,
            )
            raise PendingEffectError("pending effect is unavailable") from exc
        return self._transition(effect_id, revision, record, PendingEffectState.CLAIMED)

    def resume(
        self,
        effect_id: str,
        broker: RequestBroker,
        *,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> PendingEffectStatus:
        """Execute one claimed effect without exposing its payload or outcome.

        Broker invokes ``before_dispatch`` only after its final authority
        recheck and audit dispatch marker, immediately before it submits work
        to the provider backend.  Consequently every error after that hook is
        conservatively ambiguous and cannot be retried by this controller.
        """

        self.claim(effect_id)
        revision, record = self._load(effect_id)
        if record.state is not PendingEffectState.CLAIMED:
            raise PendingEffectError("pending effect is unavailable")

        def mark_dispatched() -> None:
            self.mark_dispatched(effect_id)

        try:
            outcome = broker.invoke_prepared(
                record.prepared,
                record.context,
                _thaw_json(record.effect_scope),
                execute_not_after_wall=record.expires_at,
                wall_clock=wall_clock,
                monotonic_clock=monotonic_clock,
                before_dispatch=mark_dispatched,
            )
        except Exception as exc:
            try:
                current_revision, current = self._load(effect_id)
                if current.state is PendingEffectState.DISPATCHED:
                    return self._transition(
                        effect_id,
                        current_revision,
                        current,
                        PendingEffectState.AMBIGUOUS,
                    )
                if current.state is PendingEffectState.CLAIMED:
                    return self._transition(
                        effect_id,
                        current_revision,
                        current,
                        PendingEffectState.STALE,
                    )
            except PendingEffectError:
                pass
            raise PendingEffectError("pending effect is unavailable") from exc
        if not isinstance(outcome, Mapping):
            raise PendingEffectError("pending effect is unavailable")
        revision, record = self._load(effect_id)
        if record.state is not PendingEffectState.DISPATCHED:
            raise PendingEffectError("pending effect is unavailable")
        return self.finish(
            effect_id,
            succeeded=True,
            outcome_digest=authority_digest(dict(outcome)),
        )

    def mark_dispatched(self, effect_id: str) -> PendingEffectStatus:
        """Durably mark the exact instant before a future provider boundary."""

        revision, record = self._load(effect_id)
        if record.state is not PendingEffectState.CLAIMED:
            raise PendingEffectError("pending effect is unavailable")
        return self._transition(
            effect_id, revision, record, PendingEffectState.DISPATCHED
        )

    def finish(
        self,
        effect_id: str,
        *,
        succeeded: bool,
        outcome_digest: str,
    ) -> PendingEffectStatus:
        """Record a terminal outcome after a future Host execution resumer."""

        revision, record = self._load(effect_id)
        if record.state is not PendingEffectState.DISPATCHED or not _is_digest(
            outcome_digest
        ):
            raise PendingEffectError("pending effect is unavailable")
        return self._transition(
            effect_id,
            revision,
            record,
            PendingEffectState.SUCCEEDED if succeeded else PendingEffectState.FAILED,
            outcome_digest=outcome_digest,
        )

    def cancel(self, effect_id: str) -> PendingEffectStatus:
        """Cancel before dispatch; dispatched work is conservatively ambiguous."""

        revision, record = self._load(effect_id)
        if record.state in _TERMINAL_STATES:
            return _status(record, revision)
        state = (
            PendingEffectState.AMBIGUOUS
            if record.state is PendingEffectState.DISPATCHED
            else PendingEffectState.CANCELLED
        )
        return self._transition(effect_id, revision, record, state)

    def recover(self) -> tuple[PendingEffectStatus, ...]:
        """Apply conservative crash semantics to every encrypted effect record."""

        recovered: list[PendingEffectStatus] = []
        for revision, payload in self._persistence.list_host_pending_effects():
            record = _PendingEffect.from_dict(payload)
            if record.state in _TERMINAL_STATES:
                recovered.append(_status(record, revision))
                continue
            if record.state in {
                PendingEffectState.PREPARED,
                PendingEffectState.APPROVAL_PENDING,
            }:
                recovered.append(self.observe_approval(record.effect_id))
                continue
            if record.state is PendingEffectState.CLAIMED:
                recovered.append(
                    self._transition(
                        record.effect_id,
                        revision,
                        record,
                        PendingEffectState.STALE,
                    )
                )
                continue
            if record.state is PendingEffectState.DISPATCHED:
                recovered.append(
                    self._transition(
                        record.effect_id,
                        revision,
                        record,
                        PendingEffectState.AMBIGUOUS,
                    )
                )
                continue
            recovered.append(_status(record, revision))
        return tuple(recovered)

    def _recover_prepared(
        self,
        effect_id: str,
        revision: int,
        record: _PendingEffect,
    ) -> PendingEffectStatus:
        """Resolve the crash window between durable snapshot and approval open."""

        try:
            approval = self._approvals.interactive_approval_status(
                record.approval_request_id
            )
        except Exception:
            return self._transition(
                effect_id, revision, record, PendingEffectState.STALE
            )
        if approval.state not in {"pending", "approved", "denied"}:
            return self._transition(
                effect_id, revision, record, PendingEffectState.STALE
            )
        pending = self._transition(
            effect_id,
            revision,
            record,
            PendingEffectState.APPROVAL_PENDING,
        )
        if pending.state is PendingEffectState.APPROVAL_PENDING:
            return self.observe_approval(effect_id)
        return pending

    def _load(self, effect_id: str) -> tuple[int, _PendingEffect]:
        """Load one encrypted record and collapse all failures to one error."""

        try:
            result = self._persistence.get_host_pending_effect(effect_id)
            if result is None:
                raise PendingEffectError("pending effect is unavailable")
            revision, payload = result
            return int(revision), _PendingEffect.from_dict(payload)
        except PendingEffectError:
            raise
        except Exception as exc:
            raise PendingEffectError("pending effect is unavailable") from exc

    def _load_owned(
        self,
        effect_id: str,
        *,
        presentation_owner_principal_id: str,
        presentation_owner_session_id: str,
    ) -> tuple[int, _PendingEffect]:
        """Load one record only when its complete owner tuple matches exactly."""

        revision, record = self._load(effect_id)
        if (
            not isinstance(presentation_owner_principal_id, str)
            or not isinstance(presentation_owner_session_id, str)
            or not secrets.compare_digest(
                record.presentation_owner_principal_id,
                presentation_owner_principal_id,
            )
            or not secrets.compare_digest(
                record.presentation_owner_session_id,
                presentation_owner_session_id,
            )
        ):
            raise PendingEffectError("pending effect is unavailable")
        return revision, record

    def _transition(
        self,
        effect_id: str,
        revision: int,
        record: _PendingEffect,
        state: PendingEffectState,
        *,
        outcome_digest: str | None = None,
    ) -> PendingEffectStatus:
        """CAS one valid lifecycle step and return only its redacted projection."""

        if not _permits_transition(record.state, state):
            raise PendingEffectError("pending effect is unavailable")
        updated = record.with_state(
            state,
            now=self._clock(),
            outcome_digest=outcome_digest,
        )
        try:
            next_revision = self._persistence.compare_and_swap_host_pending_effect(
                effect_id,
                expected_revision=revision,
                payload=updated.to_dict(),
            )
        except Exception as exc:
            raise PendingEffectError("pending effect is unavailable") from exc
        return _status(updated, next_revision)

    def _best_effort_transition(
        self,
        effect_id: str,
        revision: int,
        record: _PendingEffect,
        state: PendingEffectState,
    ) -> None:
        """Record failure when possible without masking the original denial."""

        try:
            self._transition(effect_id, revision, record, state)
        except PendingEffectError:
            pass


class LateBoundInteractiveEffectPort:
    """Fail-closed capability placeholder for the post-Broker coordinator.

    Host Provider factories are captured before the production Broker exists.
    The placeholder is therefore the only object captured by the coordinator;
    it becomes usable exactly once after the single Broker is assembled.  It
    cannot replace or rebind a live Broker across activation changes.
    """

    def __init__(self) -> None:
        self._delegate: InteractiveEffectPort | None = None
        self._lock = threading.RLock()

    def bind(self, delegate: InteractiveEffectPort) -> None:
        """Bind the one active Host implementation exactly once."""

        if delegate is self:
            raise PendingEffectError("interactive effect port is unavailable")
        with self._lock:
            if self._delegate is not None:
                raise PendingEffectError("interactive effect port is unavailable")
            self._delegate = delegate

    def prepare_interactive_effect(
        self,
        command: InteractiveEffectPrepareCommand,
    ) -> InteractiveEffectStatus:
        """Forward a prepare request only after the Broker binding exists."""

        return self._bound().prepare_interactive_effect(command)

    def get_interactive_effect(
        self,
        query: InteractiveEffectOwnerQuery,
    ) -> InteractiveEffectStatus:
        """Forward an owner-scoped redacted status request."""

        return self._bound().get_interactive_effect(query)

    def resume_interactive_effect(
        self,
        query: InteractiveEffectOwnerQuery,
    ) -> InteractiveEffectStatus:
        """Forward an owner-scoped resume request."""

        return self._bound().resume_interactive_effect(query)

    def cancel_interactive_effect(
        self,
        query: InteractiveEffectOwnerQuery,
    ) -> InteractiveEffectStatus:
        """Forward an owner-scoped cancel request."""

        return self._bound().cancel_interactive_effect(query)

    def _bound(self) -> InteractiveEffectPort:
        """Return the single bound port or reject the pre-Broker capture gap."""

        with self._lock:
            if self._delegate is None:
                raise PendingEffectError("interactive effect port is unavailable")
            return self._delegate


def _approval_request_command(
    record: _PendingEffect,
    *,
    approval_context: RequestContext,
    coordinator_publisher_lineage: str,
    typed_confirmation_phrase: str | None,
):
    """Build the narrow authority request; the phrase is never persisted here."""

    from .ports import InteractiveApprovalRequestCommand

    return InteractiveApprovalRequestCommand(
        context=approval_context,
        target_principal=_target_principal(record.prepared),
        request_digest=record.prepared.request_digest,
        base_scope=_thaw_json(record.effect_scope),
        invocation_owner_id=record.invocation_owner_id,
        presentation_owner_principal_id=record.presentation_owner_principal_id,
        presentation_owner_session_id=record.presentation_owner_session_id,
        caller_publisher_lineage=coordinator_publisher_lineage,
        target_publisher_lineage=_target_publisher_lineage(record.prepared),
        expires_at=record.expires_at,
        redacted_metadata=dict(record.presentation_metadata),
        typed_confirmation_phrase=typed_confirmation_phrase,
    )


def _grant_attestation(
    record: _PendingEffect,
    *,
    coordinator_publisher_lineage: str,
) -> InteractiveApprovalGrantAttestation:
    """Build the full no-material equality assertion for one approved record."""

    return InteractiveApprovalGrantAttestation(
        request_id=record.approval_request_id,
        context=record.context,
        target_principal=_target_principal(record.prepared),
        request_digest=record.prepared.request_digest,
        base_scope=_thaw_json(record.effect_scope),
        invocation_owner_id=record.invocation_owner_id,
        caller_publisher_lineage=coordinator_publisher_lineage,
        target_publisher_lineage=_target_publisher_lineage(record.prepared),
        expires_at=record.expires_at,
    )


def _new_effect_identifiers() -> tuple[str, str]:
    """Return unrelated opaque identifiers for state and Authority approval."""

    nonce = secrets.token_hex(18)
    return f"pending-effect-{nonce}", f"interactive-effect-{nonce}"


def _target_principal(snapshot: PreparedInvocationSnapshot) -> OpaqueAuthorityRef:
    """Read the prepared exact effect target from the canonical Broker snapshot."""

    try:
        value = snapshot.binding_fingerprint["target_principal"]
    except (KeyError, TypeError) as exc:
        raise PendingEffectError("pending effect is unavailable") from exc
    if not isinstance(value, str) or not value:
        raise PendingEffectError("pending effect is unavailable")
    return OpaqueAuthorityRef(value)


def _target_publisher_lineage(snapshot: PreparedInvocationSnapshot) -> str:
    """Read the target publisher lineage from the canonical Broker snapshot."""

    try:
        value = snapshot.binding_fingerprint["artifact"]["publisher_lineage"]
    except (KeyError, TypeError) as exc:
        raise PendingEffectError("pending effect is unavailable") from exc
    if not isinstance(value, str) or not value:
        raise PendingEffectError("pending effect is unavailable")
    return value


def _permits_transition(
    current: PendingEffectState,
    future: PendingEffectState,
) -> bool:
    """Return whether one durable state transition is safe and monotonic."""

    if current is future:
        return False
    permitted = {
        PendingEffectState.PREPARED: {
            PendingEffectState.APPROVAL_PENDING,
            PendingEffectState.STALE,
            PendingEffectState.CANCELLED,
        },
        PendingEffectState.APPROVAL_PENDING: {
            PendingEffectState.APPROVED,
            PendingEffectState.STALE,
            PendingEffectState.CANCELLED,
        },
        PendingEffectState.APPROVED: {
            PendingEffectState.CLAIMED,
            PendingEffectState.CANCELLED,
            PendingEffectState.STALE,
        },
        PendingEffectState.CLAIMED: {
            PendingEffectState.DISPATCHED,
            PendingEffectState.STALE,
            PendingEffectState.CANCELLED,
        },
        PendingEffectState.DISPATCHED: {
            PendingEffectState.SUCCEEDED,
            PendingEffectState.FAILED,
            PendingEffectState.AMBIGUOUS,
        },
    }
    return future in permitted.get(current, frozenset())


def _status(record: _PendingEffect, revision: int) -> PendingEffectStatus:
    """Project only metadata safe for a future Host presentation surface."""

    return PendingEffectStatus(
        effect_id=record.effect_id,
        approval_request_id=record.approval_request_id,
        state=record.state,
        revision=revision,
        expires_at=record.expires_at,
        presentation_metadata=dict(record.presentation_metadata),
    )


def _context_to_dict(context: RequestContext) -> dict[str, Any]:
    """Serialize the full stable future RequestContext into encrypted state."""

    return {
        "request_id": context.request_id,
        "trace_id": context.trace_id,
        "caller_principal": context.caller_principal.value,
        "profile_id": context.profile_id,
        "activation_id": context.activation_id,
        "activation_digest": context.activation_digest,
        "plan_digest": context.plan_digest,
        "security_epoch": context.security_epoch,
        "caller_session_id": context.caller_session_id,
        "caller_domain_id": context.caller_domain_id,
        "caller_boot_epoch": context.caller_boot_epoch,
        "target_domain_id": context.target_domain_id,
        "target_boot_epoch": context.target_boot_epoch,
        "target_backend_digest": context.target_backend_digest,
        "profile_authority_digest": context.profile_authority_digest,
        "fencing_token": context.fencing_token,
        "handle_namespace": context.handle_namespace,
        "profile_revision": context.profile_revision,
        "delegation_chain": [item.value for item in context.delegation_chain],
    }


def _context_from_dict(value: object) -> RequestContext:
    """Parse a full stable RequestContext from encrypted Host storage."""

    if not isinstance(value, Mapping):
        raise PendingEffectError("pending effect is unavailable")
    return RequestContext(
        request_id=str(value["request_id"]),
        trace_id=str(value["trace_id"]),
        caller_principal=OpaqueAuthorityRef(str(value["caller_principal"])),
        profile_id=str(value["profile_id"]),
        activation_id=str(value["activation_id"]),
        activation_digest=str(value["activation_digest"]),
        plan_digest=str(value["plan_digest"]),
        security_epoch=int(value["security_epoch"]),
        caller_session_id=str(value["caller_session_id"]),
        caller_domain_id=str(value["caller_domain_id"]),
        caller_boot_epoch=int(value["caller_boot_epoch"]),
        target_domain_id=str(value["target_domain_id"]),
        target_boot_epoch=int(value["target_boot_epoch"]),
        target_backend_digest=str(value["target_backend_digest"]),
        profile_authority_digest=str(value["profile_authority_digest"]),
        fencing_token=int(value["fencing_token"]),
        handle_namespace=str(value["handle_namespace"]),
        profile_revision=str(value.get("profile_revision") or ""),
        delegation_chain=tuple(
            OpaqueAuthorityRef(str(item)) for item in value.get("delegation_chain", ())
        ),
    )


def _redacted_metadata(value: Mapping[str, str]) -> Mapping[str, str]:
    """Freeze bounded display metadata and reject non-display payload shapes."""

    if not isinstance(value, Mapping) or len(value) > 32:
        raise PendingEffectError("pending effect is unavailable")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or not isinstance(item, str)
            or not key
            or len(key) > 128
            or len(item) > 2048
        ):
            raise PendingEffectError("pending effect is unavailable")
        normalized[key] = item
    return MappingProxyType(normalized)


def _frozen_json(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Freeze a canonical JSON mapping without sharing mutable caller values."""

    try:
        parsed = json.loads(canonical_json(dict(value)).decode("utf-8"))
    except Exception as exc:
        raise PendingEffectError("pending effect is unavailable") from exc
    if not isinstance(parsed, dict):
        raise PendingEffectError("pending effect is unavailable")
    return MappingProxyType(parsed)


def _thaw_json(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a private JSON copy for encrypted persistence/attestation only."""

    return json.loads(canonical_json(dict(value)).decode("utf-8"))


def _is_digest(value: str) -> bool:
    """Recognize the canonical sha256 digests used for terminal outcomes."""

    return (
        len(value) == 71
        and value.startswith("sha256:")
        and all(
            character in "0123456789abcdef"
            for character in value.removeprefix("sha256:")
        )
    )


__all__ = [
    "PendingEffectController",
    "PendingEffectError",
    "PendingEffectState",
    "PendingEffectStatus",
    "LateBoundInteractiveEffectPort",
]
