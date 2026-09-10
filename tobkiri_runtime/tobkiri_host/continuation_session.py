"""One root-owned continuation exchange, without transport or execution authority."""

from __future__ import annotations

import threading
from typing import Any, Callable

from tobkiri_protocol.canonical import strict_loads
from tobkiri_protocol.saved_tools import MAX_SAVED_TOOL_HOPS

from .continuation_chain import ChainIdentity, ContinuationChains, ResumePermit
from .continuation_envelope import (
    ValidatedContinuation,
    seal_continuation_intent,
    validate_continuation_result,
)


class ContinuationSession:
    """Couple verified framing to shared bounded, one-use continuation state.

    The root execution boundary supplies the identity and complete target plan.
    Callers still authenticate transport, preflight capabilities and dispatch
    through Authority/Broker. This class cannot execute a target or retry it.
    """

    def __init__(
        self,
        identity: ChainIdentity,
        targets: tuple[tuple[str, str], ...],
        *,
        chains: ContinuationChains,
        target_selector: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        """Capture an immutable bounded target plan, never an application plan."""
        if (
            type(targets) is not tuple
            or not 1 <= len(targets) <= 4
            or any(
                type(target) is not tuple
                or len(target) != 2
                or any(not isinstance(item, str) or not 0 < len(item) <= 512 for item in target)
                for target in targets
            )
        ):
            raise ValueError("continuation target plan is invalid")
        if target_selector is not None and not callable(target_selector):
            raise ValueError("continuation target selector is invalid")
        self._selector = target_selector
        self._limit = MAX_SAVED_TOOL_HOPS if target_selector else len(targets)
        self._identity = identity
        self._targets = targets
        self._chains = chains
        self._started = False
        self._terminal = False
        self._pending: ValidatedContinuation | None = None
        self._permit: ResumePermit | None = None
        self._previous: str | None = None
        self._resumed = False
        self._lock = threading.RLock()

    def start(self, intent: bytes, *, nonce: str) -> bytes:
        """Seal and register before the first Host action can be dispatched."""
        with self._lock:
            if self._started or self._terminal:
                raise ValueError("continuation session has already been used")
            self._started = True
            frame = seal_continuation_intent(
                intent, identity=self._identity, hop=0, previous_digest=None,
                target=self._target(0), nonce=nonce, max_hops=self._limit,
            )
            self._chains.start(self._identity, frame=frame.frame, nonce=frame.nonce)
            self._pending = frame
            return frame.frame

    def receive(self, encoded: bytes) -> ResumePermit:
        """Validate and consume one result; invalid results permanently fence it."""
        with self._lock:
            pending = self._pending
            if self._terminal or pending is None:
                raise ValueError("continuation result is not pending")
            self._pending = None
            try:
                result = validate_continuation_result(encoded, request=pending)
                permit = self._chains.take(
                    self._identity, nonce=pending.nonce, result=result.frame
                )
            except Exception:
                self.cancel()
                raise
            self._permit = permit
            self._previous = result.digest
            self._resumed = False
            return permit

    def resume_arguments(self, permit: ResumePermit) -> dict[str, Any]:
        """Return fresh ABI data only for this exact consumed result."""
        with self._lock:
            self._require_permit(permit)
            if self._resumed:
                raise ValueError("continuation resume arguments have already been taken")
            self._resumed = True
            request = strict_loads(permit.frame, max_bytes=64 * 1024, max_depth=16)
            result = strict_loads(permit.result, max_bytes=512 * 1024, max_depth=16)
            return {"state": request["state"], "outcome": result["outcome"]}

    def advance(self, permit: ResumePermit, intent: bytes, *, nonce: str) -> bytes:
        """Seal the next fixed target without renewing any request budget."""
        with self._lock:
            self._require_permit(permit)
            if not self._resumed:
                raise ValueError("continuation has not entered its resume step")
            try:
                hop = permit.hop + 1
                if hop >= self._limit:
                    raise ValueError("continuation target plan is exhausted")
                frame = seal_continuation_intent(
                    intent, identity=self._identity, hop=hop,
                    previous_digest=self._previous, target=self._target(hop), nonce=nonce,
                    max_hops=self._limit,
                )
                self._chains.advance(permit, frame=frame.frame, nonce=frame.nonce)
            except Exception:
                self.cancel()
                raise
            self._permit = None
            self._pending = frame
            return frame.frame

    def finish(self, permit: ResumePermit, outcome: bytes) -> None:
        """Account for the bounded final result and retain the replay tombstone."""
        with self._lock:
            self._require_permit(permit)
            if not self._resumed:
                raise ValueError("continuation has not entered its resume step")
            try:
                value = strict_loads(outcome, max_bytes=64 * 1024, max_depth=16)
                kind = value.get("kind") if isinstance(value, dict) else None
                if not isinstance(value, dict) or (
                    isinstance(kind, str) and kind.startswith("tobkiri.packvm.")
                ):
                    raise ValueError("continuation control frame is not a final outcome")
                self._chains.finish(permit, result=outcome)
            except Exception:
                self.cancel()
                raise
            self._terminal = True
            self._permit = None

    def cancel(self) -> None:
        """Fence local continuation state; the caller must stop active execution."""
        with self._lock:
            self._terminal = True
            self._pending = None
            self._permit = None
            try:
                self._chains.cancel(self._identity)
            except ValueError:
                # An unregistered or expired identity already has no usable
                # ledger entry. Never renew it merely to record cancellation.
                pass

    def _target(self, hop: int) -> tuple[str, str]:
        target = self._selector() if self._selector else self._targets[hop]
        if target not in self._targets:
            raise ValueError("continuation target is outside the captured plan")
        return target

    def _require_permit(self, permit: ResumePermit) -> None:
        if self._terminal or self._permit is not permit:
            raise ValueError("continuation resume permit is unavailable")
        self._chains.check_resume(permit)
