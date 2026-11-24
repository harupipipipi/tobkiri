"""Captured Workflow adapter over ordinary Authority, approval and Broker ports."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import fields, replace
import math
import secrets
import threading
from typing import TYPE_CHECKING, Any, Callable, Mapping

from core_runtime.authority.v4 import AuthorityScope
from core_runtime.workflow_v4.attempt_capacity import valid_outcome_digest
from core_runtime.workflow_v4.attempt_port import (
    CapturedWorkflowAttemptRouteV4,
    WorkflowAttemptDeclarationV4,
    WorkflowAttemptServiceConfigV4,
)
from core_runtime.workflow_v4.attempt_store import (
    WorkflowAttemptReservationMissing, WorkflowAttemptStoreV4, attempt_identity,
)
from core_runtime.workflow_v4.models import (
    ApprovalState,
    AuthorityReservation,
    DispatchAuthority,
    InvocationOutcome,
    WorkflowCancellationUnconfirmed,
    WorkflowDenied,
    digest,
)
from .data_codec import request_digest
from tobkiri_host.broker import PreparedInvocationSnapshot
from tobkiri_host.interactive_effects import PendingEffectController, PendingEffectState
from tobkiri_host.models import InvocationFrame, RequestContext
from tobkiri_host.operation_cancellation import nested_cancellation_proof_for
from tobkiri_host.ports import StaticAuthorityQuery

if TYPE_CHECKING:
    from core_runtime.host_provider_backend_v4 import HostProviderInvocationContextV4


def _context_document(context: RequestContext) -> dict[str, Any]:
    result = {field.name: getattr(context, field.name) for field in fields(context)}
    result["caller_principal"] = context.caller_principal.value
    result["delegation_chain"] = [item.value for item in context.delegation_chain]
    return result


class HostWorkflowAttemptServiceV4:
    """One captured signed route set and durable one-dispatch attempt journal.

    The opaque DispatchAuthority is only a service-local CAS claim, not an
    InvocationLease. The Broker remains responsible for every real authority,
    policy, jail, artifact, grant, runtime evidence and audit check.
    """

    def __init__(self, config: WorkflowAttemptServiceConfigV4) -> None:
        self._config = config
        self._routes = {route.key: route for route in config.routes}
        if len(self._routes) != len(config.routes) or any(
            route.caller_principal != config.coordinator_principal for route in config.routes
        ):
            raise WorkflowDenied("Workflow attempt routes are invalid")
        self._store = WorkflowAttemptStoreV4(config.state_path)

        def pending_clock() -> float:
            # Finite binary64 timestamps have a bounded JSON representation.
            now = float(config.clock())
            if not math.isfinite(now):
                raise WorkflowDenied("Workflow attempt clock is unavailable")
            return now

        self._controller = PendingEffectController(
            persistence=self._store,
            approvals=config.approvals,
            coordinator_principal=config.coordinator_principal,
            coordinator_publisher_lineage=config.coordinator_publisher_lineage,
            presentation_owner_scope=config.presentation_owner_scope,
            clock=pending_clock,
        )
        self._tokens: dict[str, tuple[str, object]] = {}
        self._active: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        # Never reconstruct ephemeral dispatch authority from a previous process.
        for revision, record in self._store.list_attempts():
            if record["state"] in {"preparing", "claimed", "dispatched"}:
                record["state"] = "stale" if record["state"] == "preparing" else "ambiguous"
                self._store.cas(record["reservation_id"], revision, record)
        self._controller.recover()

    def _guard(
        self, invocation: HostProviderInvocationContextV4, *, allow_stop: bool = False
    ) -> None:
        self._config.assert_current_capture()
        invocation.assert_current()
        envelope = invocation.envelope
        context = envelope.context
        execute = (
            (envelope.contract_id, envelope.operation_id, envelope.target_principal)
            in self._config.admitted_invocations
            and envelope.contract_id == WorkflowAttemptDeclarationV4().contract_id
            and envelope.operation_id in WorkflowAttemptDeclarationV4().operation_ids
        )
        stop = (
            allow_stop
            and envelope.target_principal in self._config.stop_principals
            and envelope.contract_id == "tobkiri.workflow.stop.v4"
            and envelope.operation_id == "run.stop"
        )
        if (
            not (execute or stop)
            or context.profile_id != self._config.profile_id
            or context.activation_id != self._config.activation_id
            or context.activation_digest != self._config.activation_digest
            or context.plan_digest != self._config.plan_digest
            or context.security_epoch != self._config.security_epoch
            or not invocation.presentation_owner_principal_id
            or not invocation.presentation_owner_session_id
            or envelope.cancellation_requested.is_set()
            or self._config.monotonic_clock() >= envelope.deadline_monotonic
        ):
            raise WorkflowDenied("Workflow attempt invocation is unavailable")

    def _owner(self, invocation: HostProviderInvocationContextV4) -> dict[str, Any]:
        return {
            "profile_id": self._config.profile_id,
            "activation_id": self._config.activation_id,
            "activation_digest": self._config.activation_digest,
            "plan_digest": self._config.plan_digest,
            "security_epoch": self._config.security_epoch,
            "principal_id": invocation.presentation_owner_principal_id,
            "session_id": invocation.presentation_owner_session_id,
        }

    def _owned(
        self, invocation: HostProviderInvocationContextV4, reservation_id: str,
        *, allow_stop: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        self._guard(invocation, allow_stop=allow_stop)
        revision, record = self._store.get(reservation_id)
        if record["owner"] != self._owner(invocation):
            raise WorkflowDenied("Workflow attempt reservation is unavailable")
        self._route(record["request"])
        return revision, record

    def _route(self, request: Mapping[str, Any]) -> CapturedWorkflowAttemptRouteV4:
        key = tuple(
            request.get(field)
            for field in (
                "contract_id",
                "contract_revision_digest",
                "operation_id",
                "function_principal_id",
            )
        )
        route = self._routes.get(key)  # type: ignore[arg-type]
        if route is None:
            raise WorkflowDenied("Workflow target has no captured signed outgoing edge")
        operation = route.binding.operation
        if (
            request.get("provider_id") != route.binding.function.function_id
            or request.get("input_schema_digest") != digest(operation.input_schema)
            or request.get("effect_ceiling") != [operation.effect_class.value]
        ):
            raise WorkflowDenied("Workflow target metadata changed")
        return route

    def _context(
        self,
        invocation: HostProviderInvocationContextV4,
        route: CapturedWorkflowAttemptRouteV4,
        request_id: str,
    ) -> RequestContext:
        context = self._config.context_for_attempt(route, invocation, request_id)
        if (
            context.caller_principal != self._config.coordinator_principal
            or context.profile_id != self._config.profile_id
            or context.activation_id != self._config.activation_id
            or context.activation_digest != self._config.activation_digest
            or context.plan_digest != self._config.plan_digest
            or context.security_epoch != self._config.security_epoch
            or context.delegation_chain
        ):
            raise WorkflowDenied("Workflow child context is unavailable")
        return replace(context, request_id=request_id, trace_id=request_id)

    def _scope(
        self,
        route: CapturedWorkflowAttemptRouteV4,
        context: RequestContext,
        request_digest: str,
        owner_id: str,
    ) -> AuthorityScope:
        ceiling = route.caller_effect_ceiling
        dimensions = dict(ceiling.dimensions)
        dimensions.update(
            {
                "invocation_owner_id": (owner_id,),
                "caller_session_id": (context.caller_session_id,),
                "plan_digest": (context.plan_digest,),
            }
        )
        scope = AuthorityScope(
            capability=ceiling.capability,
            semantics_digest=ceiling.semantics_digest,
            dimensions=dimensions,
            quotas=dict(ceiling.quotas),
            exact_request_digest=request_digest,
            opaque=ceiling.opaque,
        )
        if not scope.is_subset_of(ceiling):
            raise WorkflowDenied("Workflow attempt exceeds its signed ceiling")
        return scope

    def _reservation(
        self, record: Mapping[str, Any], state: ApprovalState,
        *, approval_request_id: str | None = None,
    ) -> AuthorityReservation:
        return AuthorityReservation(
            reservation_id=record["reservation_id"],
            state=state,
            request_digest=record["request_digest"],
            security_epoch=self._config.security_epoch,
            expires_at=record["expires_at"],
            approval_request_id=approval_request_id,
        )

    def reserve(
        self,
        invocation: HostProviderInvocationContextV4,
        run: Mapping[str, Any],
        attempt: Mapping[str, Any],
    ) -> AuthorityReservation:
        """Prepare a sealed request; existing Profile Grants need no new prompt."""
        self._guard(invocation)
        request = attempt["request"]
        if (
            request_digest(request) != attempt["request_digest"]
            or run["activation_id"] != self._config.activation_id
            or run["activation_digest"] != self._config.activation_digest
            or run["security_epoch"] != self._config.security_epoch
        ):
            raise WorkflowDenied("Workflow attempt request or activation changed")
        route = self._route(request)
        reservation_id = attempt_identity(self._config.profile_id, request["request_id"])
        context = self._context(invocation, route, reservation_id)
        prepared = self._config.broker.prepare(
            InvocationFrame(
                contract_id=route.binding.operation.contract_id,
                version_range="==" + route.binding.operation.contract_version,
                operation_id=route.binding.operation.operation_id,
                payload=request["input"],
                timeout_ms=request["timeout_ms"],
                idempotency_key=request["idempotency_key"],
            ),
            context,
        )
        if (
            prepared.binding.operation != route.binding.operation
            or prepared.binding.principal_ref != route.binding.principal_ref
            or prepared.binding.function != route.binding.function
            or prepared.binding.artifact != route.binding.artifact
        ):
            raise WorkflowDenied("Workflow prepared target changed")
        owner_id = "workflow-owner." + digest(
            {
                "owner": self._owner(invocation),
                "reservation_id": reservation_id,
            }
        ).removeprefix("sha256:")
        scope = self._scope(route, context, prepared.request_digest, owner_id)
        if route.authority_mode == "profile_grant":
            self._config.authority.check_static_path(
                StaticAuthorityQuery(
                    context,
                    route.binding.principal_ref,
                    prepared.request_digest,
                    scope.to_dict(),
                )
            )
        record = {
            "reservation_id": reservation_id,
            "owner": self._owner(invocation),
            "request": dict(request),
            "request_digest": attempt["request_digest"],
            "run_id": run["run_id"],
            "step_id": attempt["step_id"],
            "catalog_digest": run["catalog_digest"],
            "idempotency_identity": digest(
                {
                    "profile_id": self._config.profile_id,
                    "idempotency_key": request["idempotency_key"],
                }
            ),
            "state": "preparing",
            "authority_mode": route.authority_mode,
            "context": _context_document(context),
            "snapshot": prepared.to_snapshot().to_dict(),
            "effect_scope": scope.to_dict(),
            "owner_id": owner_id,
            "expires_at": self._config.clock() + 300.0,
            "effect_id": None,
        }
        if not self._store.insert(reservation_id, record):
            _, previous = self._owned(invocation, reservation_id)
            if (
                previous["request_digest"] != record["request_digest"]
                or previous["request"] != record["request"]
                or previous["context"] != record["context"]
                or previous["snapshot"] != record["snapshot"]
            ):
                raise WorkflowDenied("Workflow attempt retransmission changed")
            return self.inspect(invocation, reservation_id)
        self._guard(invocation)
        if route.authority_mode == "interactive_only":
            status = self._controller.prepare(
                prepared=prepared,
                context=context,
                effect_scope=scope.to_dict(),
                invocation_owner_id=owner_id,
                presentation_owner_principal_id=invocation.presentation_owner_principal_id,
                presentation_owner_session_id=invocation.presentation_owner_session_id,
                presentation_metadata={
                    "title": "Tobkiri Workflow step",
                    "operation_id": request["operation_id"],
                    "contract_id": request["contract_id"],
                    "request_digest": prepared.request_digest,
                },
                expires_at=record["expires_at"],
            )
            record.update(state="pending", effect_id=status.effect_id)
        else:
            record["state"] = "reserved"
        self._store.cas(reservation_id, 1, record)
        return self.inspect(invocation, reservation_id)

    def inspect(
        self, invocation: HostProviderInvocationContextV4, reservation_id: str
    ) -> AuthorityReservation:
        """Bind a fresh invocation to the same owner, snapshot and approval."""
        _, record = self._owned(invocation, reservation_id)
        if self._config.clock() >= record["expires_at"]:
            return self._reservation(record, ApprovalState.EXPIRED)
        if record["state"] in {"pending", "reserved"}:
            if record["authority_mode"] == "profile_grant":
                return self._reservation(record, ApprovalState.RESERVED)
            observed = self._controller.observe_approval(record["effect_id"])
            state = {
                PendingEffectState.APPROVAL_PENDING: ApprovalState.WAITING_APPROVAL,
                PendingEffectState.APPROVED: ApprovalState.APPROVED,
                PendingEffectState.CANCELLED: ApprovalState.DENIED,
            }.get(observed.state, ApprovalState.REVOKED)
            return self._reservation(
                record, state,
                approval_request_id=(
                    observed.approval_request_id
                    if state is ApprovalState.WAITING_APPROVAL else None
                ),
            )
        return self._reservation(record, ApprovalState.REVOKED)

    def commit(
        self,
        invocation: HostProviderInvocationContextV4,
        reservation_id: str,
        *,
        request_digest: str,
        security_epoch: int,
    ) -> DispatchAuthority:
        """Claim once, without reusing an old parent lease or minting a lease."""
        revision, record = self._owned(invocation, reservation_id)
        status = self.inspect(invocation, reservation_id)
        if (
            status.state not in {ApprovalState.RESERVED, ApprovalState.APPROVED}
            or request_digest != record["request_digest"]
            or security_epoch != self._config.security_epoch
        ):
            raise WorkflowDenied("Workflow attempt is not authorized")
        if record["authority_mode"] == "interactive_only":
            self._controller.claim(record["effect_id"])
        record["state"] = "claimed"
        self._store.cas(reservation_id, revision, record)
        token = secrets.token_hex(32)
        with self._lock:
            self._tokens[token] = (reservation_id, invocation.envelope)
        return DispatchAuthority(token, reservation_id, request_digest, security_epoch)

    def invoke(
        self,
        invocation: HostProviderInvocationContextV4,
        request: Mapping[str, Any],
        *,
        authority: DispatchAuthority,
        dispatch_fence: Callable[[str], None] | None = None,
    ) -> InvocationOutcome:
        """Run the original snapshot through the real evidence-bound Broker."""
        _, record = self._owned(invocation, authority.reservation_id)
        with self._lock:
            token = self._tokens.pop(authority.dispatch_token, None)
        if (
            token != (authority.reservation_id, invocation.envelope)
            or record["state"] != "claimed"
            or request_digest(request) != record["request_digest"]
            or authority.request_digest != record["request_digest"]
            or authority.security_epoch != self._config.security_epoch
        ):
            raise WorkflowDenied("Workflow attempt dispatch claim is unavailable")
        route = self._route(request)
        context = self._context(invocation, route, authority.reservation_id)
        if _context_document(context) != record["context"]:
            raise WorkflowDenied("Workflow child context changed")
        snapshot = PreparedInvocationSnapshot.from_dict(record["snapshot"])
        scope = self._scope(route, context, snapshot.request_digest, record["owner_id"])
        if scope.to_dict() != record["effect_scope"]:
            raise WorkflowDenied("Workflow effect ceiling changed")
        # Dispatch only the authenticated durable request after comparison.
        request = record["request"]
        signal = invocation.envelope.cancellation_requested
        stopped = threading.Event()

        def monitor() -> None:
            while not stopped.wait(0.05):
                try:
                    self._guard(invocation)
                except Exception:
                    signal.set()
                    return

        def before_dispatch() -> None:
            self._guard(invocation)
            revision, current = self._owned(invocation, authority.reservation_id)
            if current["state"] != "claimed":
                raise WorkflowDenied("Workflow attempt was already dispatched")
            if current["effect_id"] is not None:
                self._controller.mark_dispatched(current["effect_id"])
            current["state"] = "dispatched"
            self._store.cas(authority.reservation_id, revision, current)

        # Registration precedes durable admission. Stop must either find this
        # exact tracked child or win the admission fence before Broker dispatch.
        try:
            tracked_scope = invocation.cancellation.track(str(request["request_id"]))
            tracked_scope.__enter__()
        except Exception:
            return InvocationOutcome(
                error_code="cancellation_scope_unavailable", dispatched=False
            )
        watcher = threading.Thread(target=monitor, daemon=True, name="workflow-attempt-fence")
        watcher_started = False
        dispatched = False
        try:
            if dispatch_fence is not None:
                try:
                    dispatch_fence(str(request["request_id"]))
                except WorkflowDenied:
                    return InvocationOutcome(error_code="dispatch_fenced", dispatched=False)
                except Exception:
                    return InvocationOutcome(error_code="dispatch_fence_error", dispatched=False)
            with self._lock:
                self._active[authority.reservation_id] = signal
            watcher.start()
            watcher_started = True
            # The deadline is capped by the fresh live parent. The prepared
            # snapshot contains no old parent lease or cancellation authority.
            remaining = invocation.envelope.deadline_monotonic - self._config.monotonic_clock()
            deadline = min(record["expires_at"], self._config.clock() + remaining)
            evidence_scope = (
                self._config.evidence_scope(route, invocation, request)
                if self._config.evidence_scope is not None else nullcontext()
            )
            with self._config.execution_scope(context, invocation), evidence_scope:
                # From this point an exception cannot establish that no effect
                # occurred. Preserve ambiguity even if a concurrent revoke
                # already changed the durable reservation state.
                dispatched = True
                result = self._config.broker.invoke_prepared(
                    snapshot,
                    context,
                    record["effect_scope"],
                    execute_not_after_wall=deadline,
                    wall_clock=self._config.clock,
                    monotonic_clock=self._config.monotonic_clock,
                    before_dispatch=before_dispatch,
                    cancellation_requested=signal,
                    nested_cancellation_proof=nested_cancellation_proof_for(
                        invocation.envelope,
                        invocation.presentation_owner_principal_id,
                        invocation.presentation_owner_session_id,
                    ),
                    parent_deadline_monotonic=invocation.envelope.deadline_monotonic,
                    execution_guard=lambda: self._guard(invocation),
                )
            return InvocationOutcome(output=result)
        except Exception:
            return InvocationOutcome(
                error_code="workflow_broker_execution_failed",
                ambiguous_effect=dispatched,
                dispatched=dispatched,
            )
        finally:
            stopped.set()
            if watcher_started:
                watcher.join(timeout=0.1)
            with self._lock:
                self._active.pop(authority.reservation_id, None)
            tracked_scope.__exit__(None, None, None)

    def finish(
        self,
        invocation: HostProviderInvocationContextV4,
        reservation_id: str,
        *,
        outcome_digest: str,
        state: str,
    ) -> None:
        """Retain exact terminal evidence; any after-dispatch uncertainty stays fenced."""
        revision, record = self._owned(invocation, reservation_id)
        if (
            record["state"] not in {"claimed", "dispatched"}
            or not valid_outcome_digest(outcome_digest)
        ):
            raise WorkflowDenied("Workflow attempt cannot be finished")
        if state == "ambiguous_effect":
            final = "ambiguous"
            if record["effect_id"] is not None:
                self._controller.cancel(record["effect_id"])
        else:
            final = "succeeded" if state == "succeeded" else "failed"
            if record["effect_id"] is not None:
                if record["state"] == "dispatched":
                    self._controller.finish(
                        record["effect_id"],
                        succeeded=final == "succeeded",
                        outcome_digest=outcome_digest,
                    )
                else:
                    self._controller.cancel(record["effect_id"])
        record.update(state=final, outcome_digest=outcome_digest)
        self._store.cas(reservation_id, revision, record)

    def revoke(
        self, invocation: HostProviderInvocationContextV4, reservation_id: str, *, reason: str
    ) -> None:
        """Revoke only this exact presentation owner's unused reservation."""
        del reason
        revision, record = self._owned(invocation, reservation_id, allow_stop=True)
        if record["state"] in {"succeeded", "failed", "ambiguous", "cancelled", "stale"}:
            return
        record["state"] = (
            "ambiguous" if record["state"] in {"claimed", "dispatched"} else "cancelled"
        )
        if record["effect_id"] is not None:
            self._controller.cancel(record["effect_id"])
        self._store.cas(reservation_id, revision, record)
        with self._lock:
            active = self._active.get(reservation_id)
            if active is not None:
                active.set()

    def cancel(self, invocation: HostProviderInvocationContextV4, request_id: str) -> None:
        """Fence the owned reservation and require an actual Host drain proof."""
        try:
            self.revoke(
                invocation,
                attempt_identity(self._config.profile_id, request_id),
                reason="Workflow cancellation",
            )
        except WorkflowAttemptReservationMissing as error:
            # Public PENDING state precedes the private reservation. Only the
            # engine's sealed no-dispatch proof may settle this race. Ownership,
            # stale-capture and stop-principal denials must still propagate.
            raise WorkflowCancellationUnconfirmed(
                "Workflow reservation has no cancellation proof yet"
            ) from error
        try:
            binding = invocation.cancellation
            if not binding.active_for(request_id):
                raise WorkflowCancellationUnconfirmed("no live handle proves this request drained")
            if not binding.can_request(request_id):
                raise WorkflowDenied("request is not stoppable by this owner")
            observation = binding.request(request_id)
        except (WorkflowCancellationUnconfirmed, WorkflowDenied):
            raise
        except Exception as error:
            raise WorkflowCancellationUnconfirmed("cancellation proof is unavailable") from error
        if invocation.envelope.target_principal not in self._config.stop_principals:
            raise WorkflowCancellationUnconfirmed("cancellation signal is not drain confirmation")
        if not observation.wait_for_verified_drain(invocation.envelope.deadline_monotonic):
            raise WorkflowCancellationUnconfirmed("cancelled request has not verifiably drained")
