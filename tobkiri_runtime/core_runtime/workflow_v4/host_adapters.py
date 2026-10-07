"""Host-owned attempt Authority and dispatch adapters for Workflow v4.

Every adapter instance is bound to exactly one authenticated Host invocation
envelope.  Reservations are real interactive approval requests opened through
the narrow ``InteractiveApprovalPort`` — there are no synthetic grants, no
pretend dispatch tokens and no direct Provider callbacks.  Dispatch runs
through the invocation's ``contract_client`` which enters the real
``RequestBroker`` path (resolve, schema validation, adapters, admission,
materialization, lease issuance, audit, nested deadline/cancellation and
resource drain).

The approval request binds the invocation's own target — the Workflow
Function principal, which is the only principal guaranteed to live inside
the envelope's ``target_domain_id``.  A step target principal could never
satisfy the kernel's target-domain membership check from this context; the
exact step target identity (contract, operation, principal) is instead bound
inside the approval scope dimensions and the redacted metadata.  A Host
primitive which exposes the per-operation invocation context (or the per-op
target domain binding) would let the approval bind the step target's domain
directly — reported as a missing primitive rather than worked around.

The approval scope is exact-request bound (``exact_request_digest``) and
carries the kernel-required invocation dimensions.  The minted one-shot
Grant is deliberately *not* the authority carrier for the nested step
dispatch: ``exact_request_digest`` differs from the Broker request digest by
construction and its caller is the presentation user, not the Workflow
Function, so the Grant can never satisfy ``_select_grant`` and therefore
cannot widen or confuse the Broker's own authorization.  The approval is a
real, durable, human-decided gate recorded against the exact resolved
request; the Broker's normal authority lifecycle remains authoritative for
the dispatch itself.  For ``interactive_only`` edges the Broker still
requires its own interactive Grant — a Host-side edge ``caller_effect``/
``authority_mode`` exposure (a missing capture-context primitive) would be
required for the approval Grant to double as the dispatch carrier.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from core_runtime.authority.ui_operator import sign_ui_operator
from core_runtime.authority.v4_models import (
    AuthorityDenied,
    AuthorityScope,
)
from core_runtime.authority.v4_store import AuthorityStoreError
from tobkiri_host.errors import (
    AuthorizationError,
    RequestCancellationRequestedError,
    RequestTimedOutError,
)
from tobkiri_host.models import OpaqueAuthorityRef, RequestContext
from tobkiri_host.ports import (
    AuthorityApprovalWindowOpenCommand,
    InteractiveApprovalDecisionCommand,
    InteractiveApprovalGrantAttestation,
    InteractiveApprovalRequestCommand,
)

from .models import (
    WorkflowCancellationUnconfirmed,
    ApprovalState,
    AuthorityReservation,
    DispatchAuthority,
    InvocationOutcome,
    WorkflowDenied,
    digest,
)

_APPROVAL_TTL_SECONDS = 300.0
_CANCEL_ACTOR_ID = "io.tobkiri.workflow-v4.cancel"

_APPROVAL_STATES = {
    "pending": ApprovalState.WAITING_APPROVAL,
    "approved": ApprovalState.APPROVED,
    "denied": ApprovalState.DENIED,
    "expired": ApprovalState.EXPIRED,
}

# Context fields a cross-operation continuation must re-prove on the live
# envelope before the approval's original request context may be reused:
# the captured Function identity, the durable request digest, the
# Profile/activation/security-epoch identity, the presentation owner and
# session, and the publisher lineages.  Per-operation fields (domains,
# backend digest and handle namespace) legitimately differ
# between the envelope that opened the approval and the one resuming it,
# so they come from the original recorded context instead. The fencing
# token remains an exact epoch-wide identity.
_CONTINUATION_CONTEXT_FIELDS = (
    "profile_id",
    "activation_id",
    "activation_digest",
    "plan_digest",
    "profile_authority_digest",
    "profile_revision",
    "security_epoch",
    "fencing_token",
)


class HostAttemptAuthorityV4:
    """Attempt-scoped Authority backed by the real approval lifecycle."""

    def __init__(
        self,
        *,
        invocation: Any,
        approvals: Any,
        approval_window: Any | None,
        catalog_bindings: Sequence[Any],
        caller_publisher_lineage: str,
        provider_bindings: Sequence[Any] = (),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._invocation = invocation
        self._approvals = approvals
        self._approval_window = approval_window
        self._clock = clock
        self._caller_publisher_lineage = caller_publisher_lineage
        self._bindings: dict[tuple[str, str, str, str], Any] = {}
        self._function_identities: dict[str, tuple[str, str, str, str]] = {}
        self._principal_lineages: dict[str, str] = {
            # The approval target is the Workflow Function itself; it shares
            # the caller artifact's publisher lineage.
            str(invocation.envelope.target_principal.value): (
                caller_publisher_lineage
            ),
        }
        for binding in (*catalog_bindings, *provider_bindings):
            operation = binding.operation
            identity = (
                binding.artifact.digest,
                binding.function.implementation_digest,
                binding.function.function_id,
                operation.revision_digest,
            )
            principal_id = str(binding.principal_ref.value)
            previous = self._function_identities.setdefault(principal_id, identity)
            if previous != identity:
                raise WorkflowDenied("captured principal has conflicting owners")
            self._principal_lineages[principal_id] = binding.artifact.publisher_lineage
        for binding in catalog_bindings:
            operation = binding.operation
            key = (
                operation.contract_id,
                operation.revision_digest,
                operation.operation_id,
                binding.principal_ref.value,
            )
            self._bindings[key] = binding

    @property
    def _envelope(self) -> Any:
        return self._invocation.envelope

    def _binding_for(self, request: Mapping[str, Any]) -> Any:
        """Resolve the exact captured operation identity for one attempt."""
        key = (
            str(request.get("contract_id") or ""),
            str(request.get("contract_revision_digest") or ""),
            str(request.get("operation_id") or ""),
            str(request.get("function_principal_id") or ""),
        )
        binding = self._bindings.get(key)
        if binding is None:
            raise WorkflowDenied("attempt target is outside the captured catalog")
        return binding

    def _scope_for(
        self, request: Mapping[str, Any], binding: Any
    ) -> AuthorityScope:
        """Bind the captured effect ceiling plus kernel-required dimensions."""
        ceiling = tuple(
            sorted({str(binding.operation.effect_class.value)})
        )
        context = self._envelope.context
        return AuthorityScope(
            capability=ceiling[0],
            semantics_digest=str(request["effect_digest"]),
            dimensions={
                "invocation_owner_id": (
                    self._invocation.presentation_owner_principal_id,
                ),
                "caller_session_id": (str(context.caller_session_id),),
                "plan_digest": (str(context.plan_digest),),
                "effect_ceiling": ceiling,
                "target_contract_id": (
                    str(request.get("contract_id") or ""),
                ),
                "target_operation_id": (
                    str(request.get("operation_id") or ""),
                ),
                "target_principal_id": (
                    str(request.get("function_principal_id") or ""),
                ),
            },
            exact_request_digest=str(request["request_digest"]),
            opaque=True,
        )

    def _reservation(
        self,
        *,
        status: Any,
        request_digest: str,
        security_epoch: int,
    ) -> AuthorityReservation:
        """Translate the redacted approval status into an opaque reservation."""
        return AuthorityReservation(
            reservation_id=status.request_id,
            state=_APPROVAL_STATES.get(
                str(status.state), ApprovalState.REVOKED
            ),
            request_digest=request_digest,
            security_epoch=security_epoch,
            expires_at=float(status.expires_at),
        )

    def reserve(self, request: Mapping[str, Any]) -> AuthorityReservation:
        """Open one real interactive approval request for the attempt."""
        self._invocation.assert_current()
        request_id = str(request.get("request_id") or "")
        request_digest = str(request.get("request_digest") or "")
        security_epoch = int(request.get("security_epoch") or 0)
        reservation_id = f"workflow-approval-{request_id}"
        try:
            status = self._approvals.interactive_approval_status(reservation_id)
        except (AuthorityDenied, AuthorityStoreError, KeyError):
            status = None
        if status is not None:
            return self._reservation(
                status=status,
                request_digest=request_digest,
                security_epoch=security_epoch,
            )
        binding = self._binding_for(request)
        scope = self._scope_for(request, binding)
        context = replace(self._envelope.context, request_id=reservation_id)
        expires_at = float(self._clock()) + _APPROVAL_TTL_SECONDS
        status = self._approvals.request_interactive_approval(
            InteractiveApprovalRequestCommand(
                context=context,
                # The approval's authority surface is the Workflow Function
                # principal — the only principal inside this envelope's
                # target domain.  The pinned step target lives in the scope
                # dimensions and redacted metadata instead.
                target_principal=self._envelope.target_principal,
                request_digest=request_digest,
                base_scope=scope.to_dict(),
                invocation_owner_id=(
                    self._invocation.presentation_owner_principal_id
                ),
                presentation_owner_principal_id=(
                    self._invocation.presentation_owner_principal_id
                ),
                presentation_owner_session_id=(
                    self._invocation.presentation_owner_session_id
                ),
                caller_publisher_lineage=self._caller_publisher_lineage,
                target_publisher_lineage=self._caller_publisher_lineage,
                expires_at=expires_at,
                redacted_metadata={
                    "run_id": str(request.get("run_id") or ""),
                    "step_id": str(request.get("step_id") or ""),
                    "attempt_number": str(request.get("attempt_number") or ""),
                    "contract_id": str(request.get("contract_id") or ""),
                    "operation_id": str(request.get("operation_id") or ""),
                    "request_id": request_id,
                },
                typed_confirmation_phrase=None,
            )
        )
        self._persist_continuation(
            reservation_id,
            context=context,
            request_digest=request_digest,
            expires_at=expires_at,
        )
        if self._approval_window is not None:
            try:
                self._approval_window.open_authority_approval_window(
                    AuthorityApprovalWindowOpenCommand(
                        context=context,
                        request_id=reservation_id,
                        presentation_owner_principal_id=(
                            self._invocation.presentation_owner_principal_id
                        ),
                        presentation_owner_session_id=(
                            self._invocation.presentation_owner_session_id
                        ),
                    )
                )
            except Exception:
                # The window is presentation-only; a missed open can never
                # grant or skip the underlying approval request.
                pass
        return self._reservation(
            status=status,
            request_digest=request_digest,
            security_epoch=security_epoch,
        )

    def inspect(self, reservation_id: str) -> AuthorityReservation:
        """Re-evaluate the approval request and project its live state."""
        self._invocation.assert_current()
        try:
            status = self._approvals.interactive_approval_status(
                str(reservation_id)
            )
        except (
            AuthorityDenied,
            AuthorityStoreError,
            KeyError,
        ) as error:
            raise WorkflowDenied(
                "attempt approval request is absent"
            ) from error
        scope = (
            status.base_scope if isinstance(status.base_scope, Mapping) else {}
        )
        return self._reservation(
            status=status,
            request_digest=str(scope.get("exact_request_digest") or ""),
            security_epoch=int(self._envelope.context.security_epoch),
        )

    def _persist_continuation(
        self,
        reservation_id: str,
        *,
        context: Any,
        request_digest: str,
        expires_at: float,
    ) -> None:
        """Persist the approval's request context for a later continuation.

        The resume may run under a different operation envelope than the
        advance that opened the approval, so the original Host-authenticated
        context is captured into the encrypted Host-only pending-effect
        store.  A failure is fail-closed: the just-opened request is denied
        again rather than left approvable but unresumable.
        """

        snapshot = {
            "effect_id": reservation_id,
            "state": "approval_continuation",
            "kind": "workflow-v4-approval-continuation",
            "context": {
                "request_id": str(context.request_id),
                "trace_id": str(context.trace_id),
                "caller_principal": str(context.caller_principal.value),
                "profile_id": str(context.profile_id),
                "activation_id": str(context.activation_id),
                "activation_digest": str(context.activation_digest),
                "plan_digest": str(context.plan_digest),
                "profile_authority_digest": str(
                    context.profile_authority_digest
                ),
                "profile_revision": str(context.profile_revision),
                "security_epoch": int(context.security_epoch),
                "caller_session_id": str(context.caller_session_id),
                "caller_domain_id": str(context.caller_domain_id),
                "caller_boot_epoch": int(context.caller_boot_epoch),
                "target_domain_id": str(context.target_domain_id),
                "target_boot_epoch": int(context.target_boot_epoch),
                "target_backend_digest": str(context.target_backend_digest),
                "fencing_token": int(context.fencing_token),
                "handle_namespace": str(context.handle_namespace),
                "delegation_chain": [
                    str(item.value) for item in context.delegation_chain
                ],
            },
            "request_digest": request_digest,
            "target_principal_id": str(
                self._envelope.target_principal.value
            ),
            "invocation_owner_id": str(
                self._invocation.presentation_owner_principal_id
            ),
            "presentation_owner_session_id": str(
                self._invocation.presentation_owner_session_id
            ),
            "caller_publisher_lineage": self._caller_publisher_lineage,
            "target_publisher_lineage": self._caller_publisher_lineage,
            "expires_at": expires_at,
        }
        try:
            self._approvals.create_host_pending_effect(
                reservation_id, snapshot
            )
        except Exception as error:
            self.revoke(
                reservation_id,
                reason="approval_continuation_unavailable",
            )
            raise WorkflowDenied(
                "approval continuation context is unavailable"
            ) from error

    def _continuation_context(
        self,
        reservation_id: str,
        *,
        request_digest: str,
        target_lineage: str,
        approved_target_id: str,
    ) -> Any:
        """Resolve the request context a cross-operation commit must use.

        With a persisted continuation snapshot, the live envelope must first
        re-prove the same captured Function identity, request digest,
        Profile/activation/security epoch, presentation owner/session, and
        publisher lineages; the returned context then carries the original
        per-operation fields the kernel recorded for the approval.  Without
        a snapshot the current envelope is used unchanged, preserving the
        single-invocation path.
        """

        get_pending = getattr(
            self._approvals, "get_host_pending_effect", None
        )
        if get_pending is None:
            return replace(
                self._envelope.context, request_id=reservation_id
            )
        try:
            record = get_pending(reservation_id)
        except Exception as error:
            raise WorkflowDenied(
                "approval continuation context is unavailable"
            ) from error
        if record is None:
            return replace(
                self._envelope.context, request_id=reservation_id
            )
        _revision, snapshot = record
        original = (
            snapshot.get("context") if isinstance(snapshot, Mapping) else None
        )
        current = self._envelope.context
        identical = isinstance(original, Mapping) and all(
            str(original.get(field) or "")
            == str(getattr(current, field, ""))
            for field in _CONTINUATION_CONTEXT_FIELDS
        )
        if not identical or not isinstance(original, Mapping):
            raise WorkflowDenied(
                "approval continuation identity is unavailable"
            )
        original_target = str(snapshot.get("target_principal_id") or "")
        current_target = str(self._envelope.target_principal.value)
        original_identity = self._function_identities.get(original_target)
        current_identity = self._function_identities.get(current_target)
        same_function = original_target == current_target or (
            original_identity is not None and original_identity == current_identity
        )
        if (
            snapshot.get("kind") != "workflow-v4-approval-continuation"
            or snapshot.get("state") != "approval_continuation"
            or snapshot.get("effect_id") != reservation_id
            or original.get("request_id") != reservation_id
            or original_target != approved_target_id
            or not same_function
            or original.get("delegation_chain") != [
                str(item.value) for item in current.delegation_chain
            ]
            or str(original.get("caller_principal") or "")
            != str(getattr(current.caller_principal, "value", ""))
            or str(snapshot.get("request_digest") or "") != request_digest
            or str(snapshot.get("invocation_owner_id") or "")
            != str(self._invocation.presentation_owner_principal_id)
            or str(snapshot.get("presentation_owner_session_id") or "")
            != str(self._invocation.presentation_owner_session_id)
            or str(snapshot.get("caller_publisher_lineage") or "")
            != self._caller_publisher_lineage
            or str(snapshot.get("target_publisher_lineage") or "")
            != target_lineage
        ):
            raise WorkflowDenied(
                "approval continuation identity is unavailable"
            )
        return RequestContext(
            request_id=str(original.get("request_id") or reservation_id),
            trace_id=str(original.get("trace_id") or ""),
            caller_principal=OpaqueAuthorityRef(
                str(original["caller_principal"])
            ),
            profile_id=str(original["profile_id"]),
            activation_id=str(original["activation_id"]),
            activation_digest=str(original["activation_digest"]),
            plan_digest=str(original["plan_digest"]),
            security_epoch=int(original["security_epoch"]),
            caller_session_id=str(original.get("caller_session_id") or ""),
            caller_domain_id=str(original["caller_domain_id"]),
            caller_boot_epoch=int(original["caller_boot_epoch"]),
            target_domain_id=str(original["target_domain_id"]),
            target_boot_epoch=int(original["target_boot_epoch"]),
            target_backend_digest=str(original["target_backend_digest"]),
            profile_authority_digest=str(
                original["profile_authority_digest"]
            ),
            fencing_token=int(original["fencing_token"]),
            handle_namespace=str(original["handle_namespace"]),
            profile_revision=str(original.get("profile_revision") or ""),
            delegation_chain=tuple(
                OpaqueAuthorityRef(str(item))
                for item in original.get("delegation_chain") or ()
            ),
        )

    def commit(
        self,
        reservation_id: str,
        *,
        request_digest: str,
        security_epoch: int,
    ) -> DispatchAuthority:
        """Assert the approved Grant then mint a digest-bound token."""
        self._invocation.assert_current()
        if (
            type(security_epoch) is not int
            or security_epoch != self._envelope.context.security_epoch
        ):
            raise WorkflowDenied("approval continuation security epoch changed")
        try:
            status = self._approvals.interactive_approval_status(
                str(reservation_id)
            )
        except (
            AuthorityDenied,
            AuthorityStoreError,
            KeyError,
        ) as error:
            raise WorkflowDenied(
                "attempt approval request is absent"
            ) from error
        if str(status.state) != "approved":
            raise WorkflowDenied(
                f"attempt approval is {status.state}, not approved"
            )
        scope = (
            status.base_scope if isinstance(status.base_scope, Mapping) else {}
        )
        if scope.get("exact_request_digest") != request_digest:
            raise WorkflowDenied(
                "approval scope does not cover the pinned request digest"
            )
        target_id = str(status.target_principal_id or "")
        target_lineage = self._principal_lineages.get(target_id)
        if target_lineage is None:
            raise WorkflowDenied(
                "attempt approval target is outside the captured catalog"
            )
        context = self._continuation_context(
            str(reservation_id),
            request_digest=request_digest,
            target_lineage=target_lineage,
            approved_target_id=target_id,
        )
        self._approvals.assert_interactive_approval_grant(
            InteractiveApprovalGrantAttestation(
                request_id=str(reservation_id),
                context=context,
                target_principal=OpaqueAuthorityRef(target_id),
                request_digest=request_digest,
                base_scope=dict(scope),
                invocation_owner_id=(
                    self._invocation.presentation_owner_principal_id
                ),
                caller_publisher_lineage=self._caller_publisher_lineage,
                target_publisher_lineage=target_lineage,
                expires_at=float(status.expires_at),
            )
        )
        return DispatchAuthority(
            dispatch_token=digest(
                {
                    "approval_request_id": str(reservation_id),
                    "request_digest": request_digest,
                    "security_epoch": security_epoch,
                }
            ),
            reservation_id=str(reservation_id),
            request_digest=request_digest,
            security_epoch=security_epoch,
        )

    def finish(
        self,
        reservation_id: str,
        *,
        outcome_digest: str,
        state: str,
    ) -> None:
        """Verify the outcome boundary; the Broker audit remains the record."""
        self._invocation.assert_current()
        if (
            not isinstance(outcome_digest, str)
            or not outcome_digest.startswith("sha256:")
            or not isinstance(state, str)
            or not state
        ):
            raise WorkflowDenied("attempt outcome commitment is invalid")

    def revoke(self, reservation_id: str, *, reason: str) -> None:
        """Best-effort deny so a pending request cannot approve a dead attempt."""
        del reason
        self._invocation.assert_current()
        try:
            status = self._approvals.interactive_approval_status(
                str(reservation_id)
            )
        except Exception:
            return
        if status is None or str(status.state) != "pending":
            return
        try:
            snapshot = getattr(status, "request_snapshot_digest", None)
            ui_operator = (
                sign_ui_operator(
                    str(reservation_id),
                    decision="deny",
                    request_snapshot_digest=str(snapshot),
                    typed_confirmation_digest=None,
                )
                if isinstance(snapshot, str) and snapshot
                else None
            )
            self._approvals.deny_interactive_approval(
                InteractiveApprovalDecisionCommand(
                    context=replace(
                        self._envelope.context,
                        request_id=str(reservation_id),
                    ),
                    request_id=str(reservation_id),
                    actor_id=_CANCEL_ACTOR_ID,
                    confirmation_text="",
                    ui_operator=ui_operator,
                )
            )
        except Exception:
            # Fencing does not depend on the deny: a cancelled attempt can
            # never dispatch again, and the one-shot Grant expires unused.
            return


class HostAttemptInvokerV4:
    """Dispatch pinned requests through the Host ``contract_client`` only."""

    def __init__(
        self,
        *,
        invocation: Any,
        allowed_contract_ids: frozenset[str],
        consumer_pack_id: str,
        carry_evidence: bool = False,
    ) -> None:
        self._invocation = invocation
        self._allowed_contract_ids = frozenset(allowed_contract_ids)
        self._consumer_pack_id = consumer_pack_id
        self._carry_evidence = carry_evidence

    def invoke(
        self,
        request: Mapping[str, Any],
        *,
        authority: DispatchAuthority,
        dispatch_fence: Callable[[str], None] | None = None,
    ) -> InvocationOutcome:
        """Run one exact Contract request through the real Broker path.

        When the engine supplies ``dispatch_fence`` it runs after the
        tracked child registers and before dispatch: a durable cancel
        either already found the tracked child or stops dispatch here,
        so ``active_for == False`` is never mistaken for a drained attempt.
        """
        self._invocation.assert_current()
        if (
            not isinstance(authority.dispatch_token, str)
            or not authority.dispatch_token
            or not authority.reservation_id
            or not authority.request_digest
        ):
            raise WorkflowDenied("dispatch authority does not cover the request")
        try:
            binding = self._invocation.cancellation
        except Exception:
            return InvocationOutcome(
                error_code="cancellation_scope_unavailable", dispatched=False
            )
        dispatch = getattr(self._invocation, "dispatch_bounded", None)
        if dispatch is None:
            # The bounded typed dispatch API is a Host-owned primitive; when
            # the captured invocation does not offer it the attempt must stay
            # fail-closed rather than fall back to an unkeyed dispatch.
            return InvocationOutcome(
                error_code="bounded_dispatch_unavailable", dispatched=False
            )
        # Only failures that happen strictly before ``dispatch(...)`` may
        # claim ``dispatched=False``: acquiring the tracked scope and the
        # durable fence are provable pre-dispatch validation, while any
        # exception raised inside the Broker call may already follow a real
        # Provider side effect and must stay ambiguous.
        try:
            tracked_scope = binding.track(str(request["request_id"]))
            tracked_scope.__enter__()
        except Exception:
            # The tracked child never registered, so the dispatch body never
            # ran — provably undispatched.
            return InvocationOutcome(
                error_code="cancellation_scope_unavailable", dispatched=False
            )
        result: Any = None
        outcome: InvocationOutcome | None = None
        try:
            if dispatch_fence is not None:
                try:
                    dispatch_fence(str(request["request_id"]))
                except WorkflowDenied:
                    # The durable cancel won before dispatch: stop
                    # already recorded the attempt cancelled and no
                    # Provider effect can follow this point.
                    outcome = InvocationOutcome(
                        error_code="dispatch_fenced", dispatched=False
                    )
                except Exception:
                    # The fence runs strictly before dispatch, so a fence
                    # failure is still provably pre-dispatch.
                    outcome = InvocationOutcome(
                        error_code="dispatch_fence_error", dispatched=False
                    )
            if outcome is None:
                try:
                    evidence_arguments = {}
                    if self._carry_evidence:
                        from ..host_provider_backend_v4 import HostInvocationEvidenceReferenceV4
                        from .evidence import EVIDENCE_KIND
                        evidence_arguments["evidence_ref"] = HostInvocationEvidenceReferenceV4(
                            EVIDENCE_KIND, str(request["request_id"]),
                        )
                    result = dispatch(
                        allowed_contract_ids=self._allowed_contract_ids,
                        consumer_pack_id=self._consumer_pack_id,
                        contract_id=str(request["contract_id"]),
                        operation_id=str(request["operation_id"]),
                        payload=request["input"],
                        idempotency_key=str(request["idempotency_key"]),
                        timeout_ms=int(request["timeout_ms"]),
                        expected_payload_digest=digest(request["input"]),
                        **evidence_arguments,
                    )
                except RequestTimedOutError:
                    outcome = InvocationOutcome(
                        error_code="request_timed_out", timed_out=True
                    )
                except RequestCancellationRequestedError:
                    outcome = InvocationOutcome(error_code="request_cancelled")
                except (AuthorityDenied, AuthorizationError):
                    outcome = InvocationOutcome(error_code="authority_denied")
                except Exception:
                    # Post-materialization failures may or may not have taken
                    # an external effect; the Broker cannot disambiguate, so
                    # record reconciliation instead of pretending success or
                    # failure.  ValueError/PermissionError raised here land in
                    # this bucket as well — once dispatch was attempted they
                    # are no proof of a clean pre-dispatch rejection.
                    outcome = InvocationOutcome(
                        error_code="provider_dispatch_failed",
                        ambiguous_effect=True,
                    )
        except BaseException:
            tracked_scope.__exit__(*sys.exc_info())
            raise
        tracked_scope.__exit__(None, None, None)
        if outcome is None:
            if not isinstance(result, Mapping):
                outcome = InvocationOutcome(error_code="invalid_provider_outcome")
            else:
                outcome = InvocationOutcome(output=dict(result))
        return outcome

    def cancel(self, request_id: str) -> None:
        """Signal an owned child without claiming stop-role drain authority."""
        self._invocation.assert_current()
        try:
            binding = self._invocation.cancellation
        except Exception as error:
            raise WorkflowCancellationUnconfirmed(
                "operation cancellation handle is unavailable"
            ) from error
        if not binding.active_for(str(request_id)):
            raise WorkflowCancellationUnconfirmed(
                "no live handle proves this request drained"
            )
        if binding.can_request(str(request_id)):
            binding.request(str(request_id))
            raise WorkflowCancellationUnconfirmed(
                "cancellation was requested without verified drain"
            )
        raise WorkflowDenied(
            "in-flight request is not owner-stoppable from this invocation"
        )


class HostStopAttemptInvokerV4:
    """Stop-role invoker: signal tracked children and verify drain proof.

    This adapter exists only under the verified stop Function principal,
    which the cancellation registry binds with the "stop" role for the
    shared "workflow-v4" group.  ``cancel`` signals the exact tracked child
    and waits for the verified zero-child/resource-drain proof inside the
    envelope deadline — a cancelled attempt is never claimed before proof.
    """

    def __init__(self, *, invocation: Any) -> None:
        self._invocation = invocation

    def invoke(
        self,
        request: Mapping[str, Any],
        *,
        authority: DispatchAuthority,
        dispatch_fence: Callable[[str], None] | None = None,
    ) -> InvocationOutcome:
        """Never dispatch Provider work from a stop invocation."""
        del request, authority, dispatch_fence
        raise WorkflowDenied("stop invocations never dispatch Provider work")

    def cancel(self, request_id: str) -> None:
        """Signal one tracked child and require verified drain before return."""
        self._invocation.assert_current()
        try:
            binding = self._invocation.cancellation
        except Exception as error:
            raise WorkflowCancellationUnconfirmed(
                "operation cancellation handle is unavailable"
            ) from error
        if not binding.active_for(str(request_id)):
            raise WorkflowCancellationUnconfirmed(
                "no live handle proves this request drained"
            )
        if not binding.can_request(str(request_id)):
            raise WorkflowDenied(
                "in-flight request is not owner-stoppable from this invocation"
            )
        observation = binding.request(str(request_id))
        deadline = getattr(
            self._invocation.envelope, "deadline_monotonic", None
        )
        verified = (
            isinstance(deadline, (int, float))
            and not isinstance(deadline, bool)
            and observation.wait_for_verified_drain(float(deadline))
        )
        if not verified:
            raise WorkflowCancellationUnconfirmed(
                "stop could not verify child resource drain"
            )


__all__ = [
    "HostAttemptAuthorityV4",
    "HostAttemptInvokerV4",
    "HostStopAttemptInvokerV4",
]
