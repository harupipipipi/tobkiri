"""Bounded Host-owned continuation state; not a dispatch or authority API.

Not yet wired to PackVM. Callers must authenticate/version-check frames and
capture the complete request/domain/activation binding before registering a
chain. Register before dispatching its first effect. Payloads are immutable
encoded bytes; this module does not interpret or authorize their contents.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import threading
import time
from typing import Callable

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NONCE = re.compile(r"[0-9a-f]{48}\Z")


@dataclass(frozen=True)
class ChainIdentity:
    """Immutable identity captured by the authenticated execution boundary."""

    domain_id: str
    request_id: str
    binding_digest: str
    deadline: float


@dataclass(frozen=True)
class ResumePermit:
    """One local consumption result, not a serializable authorization grant."""

    identity: ChainIdentity
    hop: int
    frame: bytes
    result: bytes


@dataclass
class _Entry:
    identity: ChainIdentity
    frame: bytes
    nonce: str
    hop: int
    used_bytes: int
    seen_nonces: set[str]
    permit: ResumePermit | None = None
    terminal: bool = False


class ContinuationChains:
    """Retain pending, in-flight and terminal identities until their deadline."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 64,
        max_hops: int = 4,
        max_bytes: int = 1024 * 1024,
        max_lifetime: float = 60.0,
    ) -> None:
        """Set Host-owned bounds; callers must not take limits from guest input."""
        for limit in (max_entries, max_hops, max_bytes):
            if type(limit) is not int or limit < 1:
                raise ValueError("chain bounds must be exact positive integers")
        if (
            type(max_lifetime) not in (int, float)
            or not math.isfinite(max_lifetime)
            or max_lifetime <= 0
        ):
            raise ValueError("chain lifetime is invalid")
        self._clock = clock
        self._max_entries = max_entries
        self._max_hops = max_hops
        self._max_bytes = max_bytes
        self._max_lifetime = max_lifetime
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._lock = threading.RLock()

    def start(self, identity: ChainIdentity, *, frame: bytes, nonce: str) -> None:
        """Register before dispatch; duplicate identity cannot restart a turn."""
        with self._lock:
            self._purge()
            now = self._clock()
            if (
                not isinstance(identity, ChainIdentity)
                or not isinstance(identity.domain_id, str)
                or not identity.domain_id
                or not isinstance(identity.request_id, str)
                or not identity.request_id
                or not isinstance(identity.binding_digest, str)
                or _DIGEST.fullmatch(identity.binding_digest) is None
                or type(identity.deadline) not in (int, float)
                or not math.isfinite(identity.deadline)
                or not now < identity.deadline <= now + self._max_lifetime
            ):
                raise ValueError("chain identity or deadline is invalid")
            key = (identity.domain_id, identity.request_id)
            if key in self._entries or len(self._entries) >= self._max_entries:
                raise ValueError("chain identity is retained or capacity is exhausted")
            self._validate_frame(frame, nonce)
            self._entries[key] = _Entry(identity, frame, nonce, 0, len(frame), {nonce})

    def take(self, identity: ChainIdentity, *, nonce: str, result: bytes) -> ResumePermit:
        """Consume once before invoking the next sandboxed computation."""
        with self._lock:
            entry = self._entry(identity)
            if entry.terminal or entry.permit is not None or nonce != entry.nonce:
                raise ValueError("continuation is unavailable")
            entry.terminal = True
            self._validate_frame(result, nonce)
            if entry.used_bytes + len(result) > self._max_bytes:
                raise ValueError("continuation results exceed the byte budget")
            entry.used_bytes += len(result)
            permit = ResumePermit(entry.identity, entry.hop, entry.frame, result)
            entry.terminal = False
            entry.permit = permit
            return permit

    def advance(self, permit: ResumePermit, *, frame: bytes, nonce: str) -> None:
        """Advance without refreshing lifetime, byte budget or original binding."""
        with self._lock:
            entry = self._inflight(permit)
            # Any invalid continuation is terminal; do not allow another attempt
            # to reinterpret an uncertain result of the already consumed hop.
            entry.terminal = True
            entry.permit = None
            self._validate_frame(frame, nonce)
            if (
                entry.hop + 1 >= self._max_hops
                or entry.used_bytes + len(frame) > self._max_bytes
                or nonce in entry.seen_nonces
            ):
                raise ValueError("continuation exceeds its bounds or replays a nonce")
            entry.hop += 1
            entry.used_bytes += len(frame)
            entry.frame = frame
            entry.nonce = nonce
            entry.seen_nonces.add(nonce)
            entry.terminal = False

    def finish(self, permit: ResumePermit) -> None:
        """Finish while retaining a replay tombstone through the original deadline."""
        with self._lock:
            entry = self._inflight(permit)
            entry.terminal = True
            entry.permit = None
            entry.frame = b""

    def cancel(self, identity: ChainIdentity) -> None:
        """Fence a registered chain; stopping nested execution is the caller's job."""
        with self._lock:
            entry = self._entry(identity)
            entry.terminal = True
            entry.permit = None
            entry.frame = b""

    def _entry(self, identity: ChainIdentity) -> _Entry:
        self._purge()
        entry = self._entries.get((identity.domain_id, identity.request_id))
        if entry is None or entry.identity != identity:
            raise ValueError("chain binding is unavailable")
        return entry

    def _inflight(self, permit: ResumePermit) -> _Entry:
        entry = self._entry(permit.identity)
        if entry.terminal or entry.permit is not permit:
            raise ValueError("continuation permit is unavailable")
        return entry

    def _validate_frame(self, frame: bytes, nonce: str) -> None:
        if type(frame) is not bytes or not frame or len(frame) > self._max_bytes:
            raise ValueError("continuation frame is invalid")
        if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
            raise ValueError("continuation nonce is invalid")

    def _purge(self) -> None:
        now = self._clock()
        for key, entry in tuple(self._entries.items()):
            if entry.identity.deadline <= now:
                del self._entries[key]
