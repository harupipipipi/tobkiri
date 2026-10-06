"""Activation-local cancellation handles, never durable execution outcomes."""

from __future__ import annotations

from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
import contextvars
import math
import threading
import time
from typing import Callable, Iterator

from .broker import RequestEnvelope


@dataclass(frozen=True)
class CancellationObservation:
    """A Host-only, bounded observation of one requested cancellation."""

    completed: threading.Event
    _proof: _NestedCancellationProof

    def wait_for_verified_drain(self, deadline_monotonic: float) -> bool:
        """Return true only when this exact Host tree has drained by its deadline."""

        return self._proof.wait_for_verified_drain(deadline_monotonic)


@dataclass
class _TrackedChild:
    """One private nested Broker request registered by a live Host scope."""

    envelope: RequestEnvelope
    future: Future[object] | None = None
    cancellation_eligible: bool = False
    backend_cancelled: bool = False
    queued_cancelled: bool = False
    completed: bool = False
    resource_drained: bool = False
    independent_saved_branch: bool = False
    independent_calendar_branch: bool = False
    independent_occurrence_digest: str | None = None
    execution_proof: _NestedCancellationProof | None = None
    execution_entered: bool = False


class _NestedCancellationProof:
    """Track exact nested Broker work without exposing backend execution detail."""

    # PackVM cancellation can use a 250ms TERM grace plus a bounded KILL
    # absence check.  Two seconds remains a small UI wait against the stop
    # Contract's 30-second default while allowing that authenticated path to
    # drain under normal Host scheduling.
    _MAX_WAIT_SECONDS = 2.0
    _DEADLINE_SAFETY_MARGIN_SECONDS = 0.005

    def __init__(
        self,
        registry: OwnedCancellationHandles,
        key: tuple[object, ...],
        envelope: RequestEnvelope,
        owner_principal: str,
        owner_session: str,
    ) -> None:
        self._registry = registry
        self._key = key
        self._envelope = envelope
        self._owner_principal = owner_principal
        self._owner_session = owner_session
        self._scope_exited = False
        self._cancellation_requested = False
        self._children: dict[int, _TrackedChild] = {}
        self._next_child_id = 0
        self._verified_drained = threading.Event()
        self._execution_guard: Callable[[], None] | None = None
        self._calendar_execution_witness: Callable[[], None] | None = None

    def matches_invocation(
        self,
        envelope: RequestEnvelope,
        owner_principal: str,
        owner_session: str,
    ) -> bool:
        """Match only this live root or an exact registered, still-active child."""

        with self._registry._lock:
            if (
                owner_principal != self._owner_principal
                or owner_session != self._owner_session
                or not self._parent_available_locked()
            ):
                return False
            if envelope is self._envelope:
                return not self._scope_exited
            return any(
                child.envelope is envelope
                and self._child_active(child)
                and not envelope.cancellation_requested.is_set()
                and (
                    envelope.cancellation_requested
                    is self._envelope.cancellation_requested
                    or child.independent_saved_branch
                    or child.independent_calendar_branch
                )
                and self._matches_child_context(envelope)
                for child in self._children.values()
            )

    def validate_parent(self, cancellation_requested: threading.Event) -> None:
        """Require the exact live tree cancellation signal without mutation."""

        with self._registry._lock:
            if (
                not self._parent_available_locked()
                or cancellation_requested is not self._envelope.cancellation_requested
            ):
                raise PermissionError("nested cancellation parent is unavailable")

    def reserve_child(self, envelope: RequestEnvelope) -> int:
        """Reserve one child before submission so cancellation cannot miss it."""

        with self._registry._lock:
            self._require_live_child(envelope)
            self._next_child_id += 1
            child_id = self._next_child_id
            self._children[child_id] = _TrackedChild(envelope=envelope)
            return child_id

    def reserve_independent_saved_child(self, envelope: RequestEnvelope) -> int:
        """Reserve an exact Host-approved saved branch with its own signal.

        The Broker selects this Host-only entry after authenticating the finite
        approved delivery ancestry; no request flag selects independent mode.
        Normal children still require the exact parent cancellation Event.
        """
        return self._reserve_independent_child(envelope, calendar=False)

    def reserve_independent_calendar_child(self, envelope: RequestEnvelope) -> int:
        """Enroll only a Host-authenticated selected Calendar adapter occurrence.

        Production Broker must first prove the exact actual JobBroker dispatch
        ancestry and its retained immutable occurrence. This private entry never
        accepts a public flag and does not enable other v2 job adapters.
        """
        return self._reserve_independent_child(envelope, calendar=True)

    def _reserve_independent_child(
        self, envelope: RequestEnvelope, *, calendar: bool
    ) -> int:
        with self._registry._lock:
            self._require_live_child(envelope, independent_saved_branch=True)
            route = (
                ("tobkiri.action.job.adapter.v2", "rumi_turn_runtime_pack.chat-saved-job-adapter")
                if calendar else
                ("tobkiri.action.turn.saved.v1", "rumi_turn_runtime_pack.turn-saved")
            )
            if calendar:
                self._validate_calendar_occurrence(envelope)
            if (
                (envelope.contract_id, envelope.operation_id)
                != route
                or type(envelope.cancellation_requested) is not threading.Event
                or envelope.cancellation_requested.is_set()
                or envelope.cancellation_requested is self._envelope.cancellation_requested
                or any(
                    child.envelope.cancellation_requested
                    is envelope.cancellation_requested
                    for child in self._children.values()
                )
                or any(
                    signal is envelope.cancellation_requested
                    for signal, _completed in self._registry._active.values()
                )
                or any(
                    proof._envelope.cancellation_requested
                    is envelope.cancellation_requested
                    for proof in self._registry._records.values()
                )
            ):
                raise PermissionError("independent saved cancellation is unavailable")
            self._next_child_id += 1
            child_id = self._next_child_id
            self._children[child_id] = _TrackedChild(
                envelope=envelope, independent_saved_branch=not calendar,
                independent_calendar_branch=calendar,
                independent_occurrence_digest=(
                    self._validate_calendar_occurrence(envelope) if calendar else None
                ),
            )
            return child_id

    @staticmethod
    def _validate_calendar_occurrence(envelope: RequestEnvelope) -> str:
        import json
        from collections.abc import Mapping
        payload = envelope.payload
        fields = {"profile_id", "operation", "action_id", "payload", "idempotency_key", "schedule_id", "lease_id"}
        if (
            envelope.contract_version != "2.0.0"
            or not isinstance(payload, Mapping)
            or set(payload) - {"_session_id"} != fields
            or payload.get("operation") != "dispatch"
            or payload.get("action_id") != "chat.saved"
            or payload.get("profile_id") != envelope.context.profile_id
            or envelope.idempotency_key != payload.get("idempotency_key")
            or any(not isinstance(payload.get(field), str) or not payload[field]
                   or len(payload[field]) > 256
                   for field in fields - {"payload"})
            or not isinstance(payload.get("payload"), Mapping)
        ):
            raise PermissionError("independent Calendar occurrence is unavailable")
        task = payload["payload"]
        if (not {"profile_id", "message", "conversation_id"} <= set(task)
            or task.get("profile_id") != envelope.context.profile_id
            or not isinstance(task.get("message"), str) or not task["message"]
            or len(task["message"]) > 61440
            or (task.get("conversation_id") is not None
                and (not isinstance(task["conversation_id"], str)
                     or not task["conversation_id"]
                     or len(task["conversation_id"]) > 256))):
            raise PermissionError("independent Calendar task is unavailable")
        try:
            encoded = json.dumps(dict(payload), allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc:
            raise PermissionError("independent Calendar occurrence is invalid") from exc
        if len(encoded.encode()) > 131072:
            raise PermissionError("independent Calendar occurrence is too large")
        from tobkiri_protocol.canonical import canonical_digest
        return canonical_digest({key: value for key, value in payload.items()
                                 if key != "_session_id"})

    def abandon_child(self, child_id: int) -> None:
        """Forget a reservation when Broker submission never created a Future."""

        with self._registry._lock:
            child = self._children.get(child_id)
            if child is not None and child.future is None:
                del self._children[child_id]
                self._refresh_verified_drain_locked()

    @contextmanager
    def independent_saved_execution_scope(
        self, envelope: RequestEnvelope
    ) -> Iterator[None]:
        """Enter one enrolled Saved provider with a private branch-root proof.

        Broker must bind its real Future before provider entry. This scope adds
        no public stop handle and does not relax ordinary Event validation.
        """
        with self._independent_execution_scope(envelope, calendar=False):
            yield

    @contextmanager
    def independent_calendar_execution_scope(
        self, envelope: RequestEnvelope
    ) -> Iterator[None]:
        """Enter one exact enrolled occurrence without a public stop handle."""
        with self._independent_execution_scope(envelope, calendar=True):
            yield

    @contextmanager
    def _independent_execution_scope(
        self, envelope: RequestEnvelope, *, calendar: bool
    ) -> Iterator[None]:
        registry = self._registry
        with registry._lock:
            matches = [
                child
                for child in self._children.values()
                if child.envelope is envelope and (
                    child.independent_calendar_branch if calendar
                    else child.independent_saved_branch
                )
            ]
            route = (
                ("tobkiri.action.job.adapter.v2", "rumi_turn_runtime_pack.chat-saved-job-adapter")
                if calendar else
                ("tobkiri.action.turn.saved.v1", "rumi_turn_runtime_pack.turn-saved")
            )
            if calendar:
                self._validate_calendar_occurrence(envelope)
            if (
                len(matches) != 1
                or not self._parent_available_locked()
                or not self._matches_child_context(envelope)
                or (envelope.contract_id, envelope.operation_id)
                != route
                or envelope.cancellation_requested
                is self._envelope.cancellation_requested
                or envelope.cancellation_requested.is_set()
            ):
                raise PermissionError("independent saved execution is unavailable")
            child = matches[0]
            if calendar and child.independent_occurrence_digest != self._validate_calendar_occurrence(envelope):
                raise PermissionError("independent Calendar occurrence changed")
            if (
                child.execution_entered
                or child.future is None
                or not self._child_active(child)
            ):
                raise PermissionError(
                    "independent saved execution future is unavailable"
                )
            # An object identity cannot collide with any public reference key.
            key = (*self._key, object())
            proof = _NestedCancellationProof(
                registry,
                key,
                envelope,
                self._owner_principal,
                self._owner_session,
            )
            child.execution_entered = True
            child.execution_proof = proof
            proof._execution_guard = self._execution_guard
            proof._calendar_execution_witness = (
                self._execution_guard if calendar
                else self._calendar_execution_witness
            )
            registry._records[key] = proof
        token = _active_nested_cancellation_proof.set(proof)
        try:
            yield
        finally:
            _active_nested_cancellation_proof.reset(token)
            with registry._lock:
                proof.close_scope()
                self._refresh_verified_drain_locked()

    def bind_child(self, child_id: int, future: Future[object]) -> None:
        """Attach the exact submitted Future to its reserved child identity."""

        if not isinstance(future, Future):
            raise PermissionError("nested cancellation future is invalid")
        with self._registry._lock:
            child = self._children.get(child_id)
            if (
                child is None
                or child.future is not None
                or any(current.future is future for current in self._children.values())
            ):
                raise PermissionError("nested cancellation child is unavailable")
            child.future = future
            if self._cancellation_requested:
                # A late-bound, already-done Future has no observable ordering
                # against Stop intent.  Only still-pending work can prove drain.
                child.cancellation_eligible = not future.done()
                child.envelope.cancellation_requested.set()

        def complete(completed_future: Future[object]) -> None:
            with self._registry._lock:
                current = self._children.get(child_id)
                if current is not None and current.future is completed_future:
                    current.completed = True
                    # A child that completed before the stop request has no
                    # cancellation to prove.  Remove it under the same lock
                    # so a later request cannot be held by stale history.
                    if not self._cancellation_requested and current.resource_drained:
                        del self._children[child_id]
                    self._refresh_verified_drain_locked()

        future.add_done_callback(complete)

    def record_queued_cancellation(
        self, child_id: int, future: Future[object]
    ) -> None:
        """Record that this exact queued Future was cancelled before it ran."""

        with self._registry._lock:
            child = self._child_for_future_locked(child_id, future)
            child.queued_cancelled = True
            self._refresh_verified_drain_locked()

    def record_backend_cancellation(
        self, child_id: int, future: Future[object]
    ) -> None:
        """Record a successful authenticated backend cancellation for one child."""

        with self._registry._lock:
            child = self._child_for_future_locked(child_id, future)
            child.backend_cancelled = True
            self._refresh_verified_drain_locked()

    def record_resource_drain(self, future: Future[object]) -> None:
        """Record exact Broker admission/materialization release after child exit."""

        with self._registry._lock:
            matches = [
                (child_id, child)
                for child_id, child in self._children.items()
                if child.future is future
            ]
            if len(matches) != 1:
                if not self._cancellation_requested:
                    return
                raise PermissionError("nested cancellation child is unavailable")
            child_id, child = matches[0]
            if not child.completed or not future.done():
                raise PermissionError("nested cancellation resources are still active")
            child.resource_drained = True
            if not self._cancellation_requested:
                del self._children[child_id]
            self._refresh_verified_drain_locked()

    def request(self) -> None:
        """Start the private drain condition before signalling the outer request."""

        with self._registry._lock:
            if not self._cancellation_requested:
                for child in self._children.values():
                    child.cancellation_eligible = (
                        child.future is not None and not child.future.done()
                    )
            self._cancellation_requested = True
            # Fan out even reserved/unbound independent branches before their
            # Provider can enter. Shared signals remain harmlessly idempotent.
            for child in self._children.values():
                if child.execution_proof is not None:
                    child.execution_proof.request()
                child.envelope.cancellation_requested.set()
            self._refresh_verified_drain_locked()

    def close_scope(self) -> None:
        """Mark Host scope exit; this alone never proves child termination."""

        with self._registry._lock:
            if self._envelope.cancellation_requested.is_set():
                # Timeouts and ancestor cancellation can set the signal without
                # calling this scope's stop binding. Children still inherit it.
                self.request()
            self._scope_exited = True
            self._refresh_verified_drain_locked()

    def wait_for_verified_drain(self, deadline_monotonic: float) -> bool:
        """Wait briefly, never beyond the authenticated stop-envelope deadline."""

        if (
            type(deadline_monotonic) not in (int, float)
            or not math.isfinite(deadline_monotonic)
            or deadline_monotonic <= time.monotonic()
        ):
            return False
        timeout = min(
            self._MAX_WAIT_SECONDS,
            deadline_monotonic
            - time.monotonic()
            - self._DEADLINE_SAFETY_MARGIN_SECONDS,
        )
        return timeout > 0 and self._verified_drained.wait(timeout=timeout)

    def _require_live_child(
        self, envelope: RequestEnvelope, *, independent_saved_branch: bool = False
    ) -> None:
        if (
            not isinstance(envelope, RequestEnvelope)
            or not self._parent_available_locked()
            or envelope is self._envelope
            or any(child.envelope is envelope for child in self._children.values())
            or (
                envelope.cancellation_requested
                is not self._envelope.cancellation_requested
                and not independent_saved_branch
            )
            or not self._matches_child_context(envelope)
        ):
            raise PermissionError("nested cancellation child is unavailable")

    def _matches_child_context(self, envelope: RequestEnvelope) -> bool:
        outer = self._envelope.context
        child = envelope.context
        return (
            child.profile_id == outer.profile_id
            and child.profile_revision == outer.profile_revision
            and child.activation_id == outer.activation_id
            and child.activation_digest == outer.activation_digest
            and child.plan_digest == outer.plan_digest
            and child.profile_authority_digest == outer.profile_authority_digest
            and child.security_epoch == outer.security_epoch
            and child.fencing_token == outer.fencing_token
            and type(envelope.deadline_monotonic) in (int, float)
            and math.isfinite(envelope.deadline_monotonic)
            and envelope.deadline_monotonic > time.monotonic()
            and envelope.deadline_monotonic <= self._envelope.deadline_monotonic
        )

    @staticmethod
    def _child_active(child: _TrackedChild) -> bool:
        # Submission may enter the child before bind_child attaches its Future.
        return not child.completed and (
            child.future is None or not child.future.done()
        )

    def _parent_available_locked(self) -> bool:
        if self._execution_guard is not None:
            self._execution_guard()
        return (
            self._registry._records.get(self._key) is self
            and not self._registry._closed
            and not self._cancellation_requested
            and not self._envelope.cancellation_requested.is_set()
            and type(self._envelope.deadline_monotonic) in (int, float)
            and math.isfinite(self._envelope.deadline_monotonic)
            and self._envelope.deadline_monotonic > time.monotonic()
            and (
                not self._scope_exited
                or any(self._child_active(child) for child in self._children.values())
            )
        )

    def _child_for_future_locked(
        self, child_id: int, future: Future[object]
    ) -> _TrackedChild:
        child = self._children.get(child_id)
        if child is None or child.future is not future:
            raise PermissionError("nested cancellation child is unavailable")
        return child

    def _refresh_verified_drain_locked(self) -> None:
        if (
            self._cancellation_requested
            and self._scope_exited
            and bool(self._children)
            and all(
                child.cancellation_eligible
                and (child.backend_cancelled or child.queued_cancelled)
                and child.completed
                and child.resource_drained
                and (
                    child.execution_proof is None
                    or (child.execution_proof._scope_exited and all(
                        nested.completed and nested.resource_drained
                        for nested in child.execution_proof._children.values()
                    ))
                )
                for child in self._children.values()
            )
        ):
            self._verified_drained.set()
        # A root can leave before its exact nested Futures finish.  Retain the
        # private proof for those children, then release it after resource drain.
        if self._scope_exited and all(
            child.completed and child.resource_drained
            for child in self._children.values()
        ):
            if self._registry._records.get(self._key) is self:
                del self._registry._records[self._key]


_active_nested_cancellation_proof: (
    contextvars.ContextVar[_NestedCancellationProof | None]
) = (
    contextvars.ContextVar("active_nested_cancellation_proof", default=None)
)


def nested_cancellation_proof_for(
    envelope: RequestEnvelope,
    owner_principal: str,
    owner_session: str,
) -> _NestedCancellationProof | None:
    """Return the current exact root or enrolled child's private Host proof."""

    proof = _active_nested_cancellation_proof.get()
    if proof is None or not proof.matches_invocation(
        envelope, owner_principal, owner_session
    ):
        return None
    return proof


class OwnedCancellationHandles:
    """Retain live Events until their executing invocation leaves its scope."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: dict[
            tuple[object, ...], tuple[threading.Event, threading.Event]
        ] = {}
        self._records: dict[tuple[object, ...], _NestedCancellationProof] = {}
        self._closed = False

    @contextmanager
    def inherited_calendar_dispatch_scope(
        self, inherited_proof: _NestedCancellationProof, *, scope: object,
        target_envelope: RequestEnvelope, owner_principal: str,
        owner_session: str,
        selected_adapter_guard: Callable[[object, RequestEnvelope], None],
    ) -> Iterator[_NestedCancellationProof]:
        """Add an exact selected Calendar parent to a live enrolled Saved tree.

        The inherited proof must already own the actual JobBroker child Future.
        A separate private parent retains the selected occurrence witness; the
        original Saved proof is never repurposed as that witness or public key.
        """
        from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
        if (
            not isinstance(inherited_proof, _NestedCancellationProof)
            or inherited_proof._registry is not self
            or not isinstance(scope, CapturedInvocationScopeV4)
            or not callable(selected_adapter_guard)
            or (inherited_proof._envelope.contract_id,
                inherited_proof._envelope.operation_id)
            != ("tobkiri.action.turn.saved.v1", "rumi_turn_runtime_pack.turn-saved")
        ):
            raise PermissionError("inherited Calendar Saved proof is unavailable")

        def inherited_child() -> _TrackedChild:
            if not inherited_proof.matches_invocation(
                scope.envelope, owner_principal, owner_session,
            ):
                raise PermissionError("inherited Calendar source scope differs")
            matches = [child for child in inherited_proof._children.values()
                       if child.envelope is scope.envelope]
            if (len(matches) != 1 or matches[0].future is None
                or not inherited_proof._child_active(matches[0])
                or scope.envelope.cancellation_requested
                is not inherited_proof._envelope.cancellation_requested):
                raise PermissionError("inherited Calendar source Future is unavailable")
            return matches[0]

        def assert_selected(actual_scope: object, actual_target: RequestEnvelope) -> None:
            with self._lock:
                inherited_child()
            if actual_scope is not scope or actual_target is not target_envelope:
                raise PermissionError("inherited Calendar physical scope changed")
            selected_adapter_guard(actual_scope, actual_target)

        with self._lock:
            child = inherited_child()
            if child.execution_proof is not None or child.execution_entered:
                raise PermissionError("inherited Calendar source already entered")
        with self.private_calendar_dispatch_scope(
            scope=scope, target_envelope=target_envelope,
            owner_principal=owner_principal, owner_session=owner_session,
            selected_adapter_guard=assert_selected,
        ) as selected_proof:
            with self._lock:
                current = inherited_child()
                if (current is not child or current.execution_proof is not None
                    or current.execution_entered):
                    raise PermissionError("inherited Calendar source registration changed")
                # Exact parent stop now immediately fans into the independent
                # occurrence; verified parent drain retains its private scope.
                child.execution_entered = True
                child.execution_proof = selected_proof
            yield selected_proof

    @contextmanager
    def private_calendar_dispatch_scope(
        self, *, scope: object, target_envelope: RequestEnvelope,
        owner_principal: str, owner_session: str,
        selected_adapter_guard: Callable[[object, RequestEnvelope], None],
    ) -> Iterator[_NestedCancellationProof]:
        """Track a fresh actual JobBroker parent without a public stop handle.

        The production-owned witness must compare ``scope`` by object identity
        with its retained invocation and verify the actual selected signed
        adapter target. No contract payload can supply this callable or scope.
        The child remains separately enrolled and must bind its real Future.
        """
        from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
        from tobkiri_protocol.canonical import canonical_digest
        if (
            not isinstance(scope, CapturedInvocationScopeV4)
            or not isinstance(target_envelope, RequestEnvelope)
            or not callable(selected_adapter_guard)
            or any(not isinstance(value, str) or not value
                   for value in (owner_principal, owner_session))
        ):
            raise PermissionError("private Calendar parent scope is unavailable")
        envelope = scope.envelope
        fields = {"profile_id", "operation", "action_id", "payload",
                  "idempotency_key", "schedule_id", "lease_id"}
        if (
            (envelope.contract_id, envelope.operation_id)
            != ("tobkiri.action.job.v1", "rumi_job_action_broker_pack.job-action-broker")
            or envelope.contract_version != "1.0.0"
            or set(envelope.payload) - {"_session_id"} != fields
            or envelope.payload.get("operation") != "dispatch"
            or (target_envelope.contract_id, target_envelope.operation_id)
            != ("tobkiri.action.job.adapter.v2", "rumi_turn_runtime_pack.chat-saved-job-adapter")
        ):
            raise PermissionError("private Calendar parent route is unavailable")
        occurrence_digest = _NestedCancellationProof._validate_calendar_occurrence(target_envelope)
        if canonical_digest({key: value for key, value in envelope.payload.items()
                             if key != "_session_id"}) != occurrence_digest:
            raise PermissionError("private Calendar parent occurrence differs")
        key = (object(),)
        proof = _NestedCancellationProof(
            self, key, envelope, owner_principal, owner_session,
        )

        def assert_selected() -> None:
            scope.assert_current()
            selected_adapter_guard(scope, target_envelope)
            if (
                _NestedCancellationProof._validate_calendar_occurrence(target_envelope)
                != occurrence_digest
                or canonical_digest({key: value for key, value in envelope.payload.items()
                                     if key != "_session_id"}) != occurrence_digest
            ):
                raise PermissionError("private Calendar captured occurrence changed")

        with self._lock:
            assert_selected()
            if (self._closed or envelope.cancellation_requested.is_set()
                or target_envelope.cancellation_requested is envelope.cancellation_requested
                or not proof._matches_child_context(target_envelope)
                or any(record._envelope is envelope for record in self._records.values())):
                raise PermissionError("private Calendar parent is already unavailable")
            proof._execution_guard = assert_selected
            self._records[key] = proof
        token = _active_nested_cancellation_proof.set(proof)
        try:
            yield proof
        finally:
            _active_nested_cancellation_proof.reset(token)
            with self._lock:
                proof.close_scope()

    def bind(
        self,
        *,
        group: tuple[str, str],
        role: str,
        envelope: RequestEnvelope,
        owner_principal: str,
        owner_session: str,
        guard: Callable[[], None],
    ) -> OwnedCancellationBinding:
        """Bind Host-verified factory routing and presentation ownership."""
        if (
            role not in {"execute", "stop"}
            or len(group) != 2
            or not all(isinstance(value, str) and value for value in group)
            or not isinstance(owner_principal, str) or not owner_principal
            or not isinstance(owner_session, str) or not owner_session
            or not isinstance(envelope, RequestEnvelope)
            or type(envelope.cancellation_requested) is not threading.Event
        ):
            raise PermissionError("operation cancellation binding is unavailable")
        context = envelope.context
        scope = (
            *group,
            owner_principal,
            owner_session,
            context.profile_id,
            context.profile_revision,
            context.activation_id,
            context.activation_digest,
            context.plan_digest,
            context.profile_authority_digest,
            context.security_epoch,
            context.fencing_token,
        )
        return OwnedCancellationBinding(
            self,
            scope,
            role,
            envelope,
            owner_principal,
            owner_session,
            guard,
        )

    def close(self) -> None:
        """Request shutdown, retaining handles until each executor actually exits."""
        with self._lock:
            self._closed = True
            for signal, _completed in self._active.values():
                signal.set()
            for proof in list(self._records.values()):
                proof.request()
                proof._envelope.cancellation_requested.set()


class OwnedCancellationBinding:
    """One invocation's role-limited view of its owner's live handles."""

    def __init__(
        self, registry: OwnedCancellationHandles, scope: tuple[object, ...], role: str,
        envelope: RequestEnvelope, owner_principal: str, owner_session: str,
        guard: Callable[[], None],
    ) -> None:
        self._registry, self._scope, self._role = registry, scope, role
        self._envelope = envelope
        self._signal, self._guard = envelope.cancellation_requested, guard
        self._owner_principal, self._owner_session = owner_principal, owner_session

    def _key(self, reference: str) -> tuple[object, ...]:
        if (
            not isinstance(reference, str) or not 0 < len(reference) <= 256
            or reference.strip() != reference
        ):
            raise ValueError("operation reference is invalid")
        return (*self._scope, reference)

    @contextmanager
    def track(self, reference: str) -> Iterator[None]:
        """Register only while the bound execution is alive; never replace a handle."""
        key = self._key(reference)
        registry = self._registry
        with registry._lock:
            # Lock contention may outlive the captured invocation.
            self._guard()
            if (
                self._role != "execute" or registry._closed
                or key in registry._active or key in registry._records
                or self._signal.is_set()
            ):
                raise PermissionError("operation cancellation handle is unavailable")
            inherited = _active_nested_cancellation_proof.get()
            calendar_guard: Callable[[], None] | None = None
            if (
                type(inherited) is _NestedCancellationProof
                and inherited._calendar_execution_witness is not None
            ):
                if (
                    inherited._registry is not registry
                    or not inherited.matches_invocation(
                        self._envelope, self._owner_principal, self._owner_session,
                    )
                ):
                    raise PermissionError("Calendar execution witness does not match track")
                witness = inherited._calendar_execution_witness

                def assert_calendar_track() -> None:
                    self._guard()
                    witness()
                    if not inherited.matches_invocation(
                        self._envelope, self._owner_principal, self._owner_session,
                    ):
                        raise PermissionError("Calendar tracked parent is unavailable")

                calendar_guard = assert_calendar_track
                calendar_guard()
            completed = threading.Event()
            proof = _NestedCancellationProof(
                registry, key, self._envelope, self._owner_principal,
                self._owner_session,
            )
            if calendar_guard is not None:
                proof._execution_guard = calendar_guard
                proof._calendar_execution_witness = calendar_guard
            registry._active[key] = (self._signal, completed)
            registry._records[key] = proof
        context_token = _active_nested_cancellation_proof.set(proof)
        try:
            yield
        finally:
            _active_nested_cancellation_proof.reset(context_token)
            with registry._lock:
                current = registry._active.get(key)
                if current == (self._signal, completed):
                    proof.close_scope()
                    completed.set()
                    del registry._active[key]

    def active_for(self, reference: str) -> bool:
        """Report whether any owner still tracks this reference in this group."""
        key = self._key(reference)
        group = self._scope[:2]
        with self._registry._lock:
            return any(
                active[-1] == key[-1] and active[:2] == group
                for active in self._registry._active
            )

    def can_request(self, reference: str) -> bool:
        """Authorize this stop scope for one exact owned live execution."""

        key = self._key(reference)
        registry = self._registry
        with registry._lock:
            self._guard()
            return (
                self._role == "stop"
                and not registry._closed
                and key in registry._active
                and key in registry._records
            )

    def request(self, reference: str) -> CancellationObservation:
        """Signal one owned live execution and expose only its scope-exit event."""
        key = self._key(reference)
        registry = self._registry
        with registry._lock:
            self._guard()
            if self._role != "stop" or registry._closed or key not in registry._active:
                raise PermissionError("operation cancellation handle is unavailable")
            signal, completed = registry._active[key]
            proof = registry._records.get(key)
            if proof is None:
                raise PermissionError("operation cancellation handle is unavailable")
            proof.request()
            signal.set()
            return CancellationObservation(completed=completed, _proof=proof)
