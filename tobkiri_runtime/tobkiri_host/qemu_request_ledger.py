"""Bounded exchange ownership; cancellation cannot be revived by late replies."""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from .errors import BackendUnavailableError


@dataclass(frozen=True)
class QemuRequestTicket:
    """One exclusive transport exchange within a finite guest operation."""

    request_id: str
    request_digest: str
    generation: str
    exchange: str
    deadline: float


@dataclass
class _Entry:
    ticket: QemuRequestTicket
    remaining_bridges: int
    in_flight: bool = True


class QemuRequestLedger:
    """Limit active calls and preserve cancellation across registration races."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}
        self._cancelled: dict[str, float] = {}
        self._overflow_until = 0.0

    def begin(
        self, request_id: str, request_digest: str, deadline: float, *, maximum_bridges: int
    ) -> QemuRequestTicket:
        """Reserve a new operation exactly once while its ticket is live."""
        now = self._clock()
        with self._lock:
            self._entries = {
                key: val for key, val in self._entries.items() if val.ticket.deadline > now
            }
            self._cancelled = {key: val for key, val in self._cancelled.items() if val > now}
            if (
                request_id in self._entries
                or request_id in self._cancelled
                or len(self._entries) >= 128
                or len(self._cancelled) >= 128
                or self._overflow_until > now
                or deadline <= now
                or maximum_bridges not in (1, 4, 16)
            ):
                raise BackendUnavailableError(
                    "QEMU request identity, deadline or ledger bound rejected"
                )
            ticket = QemuRequestTicket(
                request_id, request_digest, secrets.token_hex(16), secrets.token_hex(16), deadline
            )
            self._entries[request_id] = _Entry(ticket, maximum_bridges)
            return ticket

    def resume(self, request_id: str) -> QemuRequestTicket:
        """Consume one previously pending exchange without allowing concurrency."""
        with self._lock:
            entry = self._entries.get(request_id)
            if (
                entry is None
                or entry.in_flight
                or entry.ticket.deadline <= self._clock()
                or entry.remaining_bridges < 0
            ):
                raise BackendUnavailableError("QEMU continuation is not pending or has expired")
            entry.ticket = replace(entry.ticket, exchange=secrets.token_hex(16))
            entry.in_flight = True
            return entry.ticket

    def settle(self, ticket: QemuRequestTicket, *, pending: bool) -> None:
        """Settle only after signature verification and before exposing a result."""
        with self._lock:
            entry = self._entries.get(ticket.request_id)
            if (
                entry is None
                or entry.ticket != ticket
                or not entry.in_flight
                or ticket.deadline <= self._clock()
            ):
                raise BackendUnavailableError("QEMU late or cancelled exchange cannot settle")
            if not pending:
                del self._entries[ticket.request_id]
            elif entry.remaining_bridges > 0:
                entry.remaining_bridges -= 1
                entry.in_flight = False
            else:
                del self._entries[ticket.request_id]
                raise BackendUnavailableError("QEMU continuation hop bound exhausted")

    def abandon(self, ticket: QemuRequestTicket) -> None:
        """Retire a failed exchange without affecting a newer generation."""
        with self._lock:
            entry = self._entries.get(ticket.request_id)
            if entry is not None and entry.ticket == ticket:
                del self._entries[ticket.request_id]

    def cancel(self, request_id: str, request_digest: str) -> None:
        """Fence before guest cancellation, including cancel-before-begin races."""
        now = self._clock()
        with self._lock:
            entry = self._entries.get(request_id)
            if entry is not None and entry.ticket.request_digest != request_digest:
                raise BackendUnavailableError("QEMU cancellation request digest mismatch")
            self._entries.pop(request_id, None)
            self._cancelled = {key: val for key, val in self._cancelled.items() if val > now}
            if request_id in self._cancelled or len(self._cancelled) < 128:
                self._cancelled[request_id] = now + 605
            else:
                self._overflow_until = max(self._overflow_until, now + 605)
