"""Host-private propagation of exact saved-tool admission guards."""

from contextlib import contextmanager
from threading import RLock
from typing import Any, Callable, Iterator


class SavedToolEntryGuardRegistry:
    """Retain proof guards only during an exact original Broker invocation."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[str, tuple[Any, str, Callable[[], None], object]] = {}

    @contextmanager
    def register(
        self, context: Any, digest: str, proof_guard: Callable[[], None]
    ) -> Iterator[None]:
        """Bind a private guard to one retained context and request digest."""
        if not digest or not callable(proof_guard):
            raise PermissionError("saved tool entry guard is invalid")
        token = object()
        key = context.request_id
        with self._lock:
            if key in self._entries:
                raise PermissionError("saved tool entry guard already registered")
            self._entries[key] = (context, digest, proof_guard, token)
        try:
            proof_guard()
            yield
        finally:
            with self._lock:
                if self._entries.get(key, (None, None, None, None))[3] is token:
                    del self._entries[key]

    def capture(self, envelope: Any) -> Callable[[], None] | None:
        """Capture an exact private guard for repeated scope assertions."""
        key = envelope.context.request_id
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            context, digest, guard, token = entry
            if envelope.context != context or envelope.request_digest != digest:
                raise PermissionError("saved tool entry capture changed")

        def assert_current() -> None:
            with self._lock:
                current = self._entries.get(key)
                if current is None or current[3] is not token:
                    raise PermissionError("saved tool entry proof is no longer active")
                if envelope.context != context or envelope.request_digest != digest:
                    raise PermissionError("saved tool entry capture changed")
            guard()

        return assert_current
