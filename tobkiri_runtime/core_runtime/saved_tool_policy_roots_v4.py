"""Private current-turn root resolution backed by authenticated native receipts."""

from __future__ import annotations

from threading import RLock
from typing import Any, Callable

from tobkiri_protocol.canonical import canonical_digest


class SavedToolPolicyRootsV4:
    """Keep root references while repeating native proof on every operation.

    References are never authorization. ``create`` must complete the actual
    native selected-policy route and return a committed proof-bearing root;
    ``restore`` must rehydrate authenticated encrypted receipts, not UI state.
    """

    def __init__(
        self,
        *,
        capture_context: Callable[[Any], Any],
        create: Callable[[Any, Any], Any],
        restore: Callable[[Any, Any], Any | None],
    ) -> None:
        self._capture_context = capture_context
        self._create = create
        self._restore = restore
        self._lock = RLock()
        self._roots: dict[str, Any] = {}
        self._selecting: set[str] = set()

    @staticmethod
    def _key(context: Any) -> str:
        return canonical_digest(
            {
                "capture": context.capture_digest,
                "mode": context.mode,
                "conversation": context.conversation_id,
                "turn": context.turn_id,
                "workspace": context.workspace_id,
                "owner_principal": context.root.caller_principal.value,
                "owner_session": context.root.caller_session_id,
                "saved_request_id": context.root.request_id,
            }
        )

    def capture_current(self, invocation: Any, authenticated_root: Any) -> Any:
        """Create native selection once, then repeat actual root and saved proof."""
        context = self._capture_context(invocation)
        context.assert_current()
        if context.root != authenticated_root or context.mode not in {"agent", "full"}:
            raise PermissionError("saved policy root ancestry changed")
        key = self._key(context)
        with self._lock:
            root = self._roots.get(key)
            if root is None:
                if key in self._selecting:
                    raise PermissionError("native policy selection already pending")
                self._selecting.add(key)
        if root is None:
            try:
                # Restoring a revoked/expired receipt must raise rather than
                # return None and quietly create another native selection.
                root = self._restore(invocation, context)
                if root is None:
                    root = self._create(invocation, context)
                context.assert_current()
                root.capture_current(invocation, authenticated_root).assert_current()
                with self._lock:
                    self._roots[key] = root
            finally:
                with self._lock:
                    self._selecting.discard(key)
        result = root.capture_current(invocation, authenticated_root)
        result.assert_current()
        if (
            result.mode != context.mode
            or result.capture_digest != context.capture_digest
            or result.turn_id != context.turn_id
            or result.workspace_root != str(context.workspace_binding.canonical_root)
        ):
            raise PermissionError("native policy selected root changed")
        return result

    def admission(
        self,
        invocation: Any,
        *,
        requested_mode: Callable[[Any], str],
        available_modes: Callable[[], tuple[str, ...]],
    ) -> str:
        """Check requested support before every branch, including native create."""
        mode = requested_mode(invocation)
        if mode not in {"ask", "agent", "full"}:
            raise PermissionError("unsupported saved tool requested mode")
        if mode not in available_modes():
            raise PermissionError("saved tool requested policy unavailable")
        return mode
