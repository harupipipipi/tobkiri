"""Host-private selected policy inheritance through actual invocation scopes."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable, Iterator


@dataclass(frozen=True)
class InheritedToolPolicy:
    """One authentic selected policy plus the child's actual opaque parent lease."""

    policy: Any
    parent_lease: Any
    assert_current: Callable[[], None]


class PolicyInvocationRegistry:
    """Retain a native root only during one exact original Broker invocation."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[str, tuple[Any, str, Any, Callable[[], None], object]] = {}

    @contextmanager
    def register(
        self,
        context: Any,
        request_digest: str,
        policy: Any,
        proof_guard: Callable[[], None],
    ) -> Iterator[None]:
        """Register a current Host-native selected policy without wire metadata."""
        policy.assert_current()
        proof_guard()
        if not request_digest or policy.mode not in {"agent", "full"}:
            raise PermissionError("selected policy inheritance is unavailable")
        token = object()
        with self._lock:
            if context.request_id in self._entries:
                raise PermissionError("selected policy inheritance already retained")
            self._entries[context.request_id] = (
                context,
                request_digest,
                policy,
                proof_guard,
                token,
            )
        try:
            yield
        finally:
            with self._lock:
                current = self._entries.get(context.request_id)
                if current is not None and current[4] is token:
                    del self._entries[context.request_id]

    def capture_current(self, invocation: Any) -> InheritedToolPolicy | None:
        """Resolve only actual current ancestry; arbitrary selection IDs cannot bind."""
        invocation.assert_current()
        envelopes = [invocation.envelope]
        parents: list[Any] = []
        scope = invocation.parent_invocation
        while scope is not None:
            if len(parents) >= 16 or any(scope is item for item in parents):
                raise PermissionError("selected policy ancestry is invalid")
            scope.assert_current()
            parents.append(scope)
            envelopes.append(scope.envelope)
            scope = scope.parent
        matched = []
        with self._lock:
            for envelope in envelopes:
                entry = self._entries.get(envelope.context.request_id)
                if entry is None:
                    continue
                if envelope.context != entry[0] or envelope.request_digest != entry[1]:
                    raise PermissionError("selected policy ancestry capture changed")
                matched.append((envelope, entry))
        if not matched:
            return None
        if len(matched) != 1:
            raise PermissionError("selected policy ancestry is ambiguous")
        envelope, entry = matched[0]
        context, digest, policy, proof_guard, token = entry

        def guard() -> None:
            invocation.assert_current()
            with self._lock:
                current = self._entries.get(context.request_id)
                if current is None or current[4] is not token:
                    raise PermissionError("selected policy inheritance drained")
                if envelope.context != context or envelope.request_digest != digest:
                    raise PermissionError("selected policy ancestry capture changed")
            policy.assert_current()
            proof_guard()

        guard()
        return InheritedToolPolicy(
            policy=policy,
            parent_lease=invocation.envelope.lease,
            assert_current=guard,
        )
