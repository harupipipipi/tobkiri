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

    future: Future[object] | None = None
    backend_cancelled: bool = False
    queued_cancelled: bool = False
    completed: bool = False


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

    def matches_outer(
        self,
        envelope: RequestEnvelope,
        owner_principal: str,
        owner_session: str,
    ) -> bool:
        """Return whether this private proof belongs to the exact outer call."""

        return (
            envelope is self._envelope
            and owner_principal == self._owner_principal
            and owner_session == self._owner_session
        )

    def validate_parent(self, cancellation_requested: threading.Event) -> None:
        """Require the exact live outer cancellation signal without mutation."""

        with self._registry._lock:
            if (
                self._registry._records.get(self._key) is not self
                or self._scope_exited
                or self._cancellation_requested
                or cancellation_requested is not self._envelope.cancellation_requested
                or cancellation_requested.is_set()
            ):
                raise PermissionError("nested cancellation parent is unavailable")

    def reserve_child(self, envelope: RequestEnvelope) -> int:
        """Reserve one child before submission so cancellation cannot miss it."""

        with self._registry._lock:
            self._require_live_child(envelope)
            self._next_child_id += 1
            child_id = self._next_child_id
            self._children[child_id] = _TrackedChild()
            return child_id

    def abandon_child(self, child_id: int) -> None:
        """Forget a reservation when Broker submission never created a Future."""

        with self._registry._lock:
            child = self._children.get(child_id)
            if child is not None and child.future is None:
                del self._children[child_id]
                self._refresh_verified_drain_locked()

    def bind_child(self, child_id: int, future: Future[object]) -> None:
        """Attach the exact submitted Future to its reserved child identity."""

        if not isinstance(future, Future):
            raise PermissionError("nested cancellation future is invalid")
        with self._registry._lock:
            child = self._children.get(child_id)
            if child is None or child.future is not None:
                raise PermissionError("nested cancellation child is unavailable")
            child.future = future

        def complete(completed_future: Future[object]) -> None:
            with self._registry._lock:
                current = self._children.get(child_id)
                if current is not None and current.future is completed_future:
                    current.completed = True
                    # A child that completed before the stop request has no
                    # cancellation to prove.  Remove it under the same lock
                    # so a later request cannot be held by stale history.
                    if not self._cancellation_requested:
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

    def request(self) -> None:
        """Start the private drain condition before signalling the outer request."""

        with self._registry._lock:
            self._cancellation_requested = True
            self._refresh_verified_drain_locked()

    def close_scope(self) -> None:
        """Mark Host scope exit; this alone never proves child termination."""

        with self._registry._lock:
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

    def _require_live_child(self, envelope: RequestEnvelope) -> None:
        if (
            self._registry._records.get(self._key) is not self
            or self._scope_exited
            or self._cancellation_requested
            or self._envelope.cancellation_requested.is_set()
            or envelope is self._envelope
            or (
                envelope.cancellation_requested
                is not self._envelope.cancellation_requested
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
            and envelope.deadline_monotonic <= self._envelope.deadline_monotonic
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
            and all(
                (child.backend_cancelled or child.queued_cancelled)
                and child.completed
                for child in self._children.values()
            )
        ):
            self._verified_drained.set()


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
    """Return the current exact Host scope's private nested-cancellation proof."""

    proof = _active_nested_cancellation_proof.get()
    if proof is None or not proof.matches_outer(
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
                self._role != "execute" or registry._closed or key in registry._active
                or self._signal.is_set()
            ):
                raise PermissionError("operation cancellation handle is unavailable")
            completed = threading.Event()
            proof = _NestedCancellationProof(
                registry, key, self._envelope, self._owner_principal,
                self._owner_session,
            )
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
                    registry._records.pop(key, None)

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
