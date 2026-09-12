"""Activation-local cancellation handles, never durable execution outcomes."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import threading
from typing import Callable, Iterator

from .broker import RequestEnvelope


@dataclass(frozen=True)
class CancellationObservation:
    """Host-owned observation that the signalled execution left its live scope."""

    completed: threading.Event


class OwnedCancellationHandles:
    """Retain live Events until their executing invocation leaves its scope."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: dict[tuple[object, ...], tuple[threading.Event, threading.Event]] = {}
        self._closed = False

    def bind(
        self, *, group: tuple[str, str], role: str, envelope: RequestEnvelope,
        owner_principal: str, owner_session: str, guard: Callable[[], None],
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
            *group, owner_principal, owner_session, context.profile_id,
            context.profile_revision, context.activation_id, context.activation_digest,
            context.plan_digest, context.profile_authority_digest, context.security_epoch,
        )
        return OwnedCancellationBinding(
            self, scope, role, envelope.cancellation_requested, guard,
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
        signal: threading.Event, guard: Callable[[], None],
    ) -> None:
        self._registry, self._scope, self._role = registry, scope, role
        self._signal, self._guard = signal, guard

    def _key(self, reference: str) -> tuple[object, ...]:
        if (
            not isinstance(reference, str) or not 0 < len(reference) <= 256
            or reference.strip() != reference
        ):
            raise ValueError("operation reference is invalid")
        self._guard()
        return (*self._scope, reference)

    @contextmanager
    def track(self, reference: str) -> Iterator[None]:
        """Register only while the bound execution is alive; never replace a handle."""
        key = self._key(reference)
        registry = self._registry
        with registry._lock:
            if self._role != "execute" or registry._closed or key in registry._active:
                raise PermissionError("operation cancellation handle is unavailable")
            completed = threading.Event()
            registry._active[key] = (self._signal, completed)
        try:
            yield
        finally:
            with registry._lock:
                current = registry._active.get(key)
                if current == (self._signal, completed):
                    completed.set()
                    del registry._active[key]

    def request(self, reference: str) -> CancellationObservation:
        """Signal one owned live execution and expose only its scope-exit event."""
        key = self._key(reference)
        registry = self._registry
        with registry._lock:
            if self._role != "stop" or registry._closed or key not in registry._active:
                raise PermissionError("operation cancellation handle is unavailable")
            signal, completed = registry._active[key]
            signal.set()
            return CancellationObservation(completed=completed)
