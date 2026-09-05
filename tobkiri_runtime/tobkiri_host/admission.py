"""Static admission, fair bounded queues, and reservation accounting."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import secrets
from threading import RLock
import time
from typing import Callable, Mapping

from .errors import AdmissionError, QueueFullError, ResourceExhaustedError


@dataclass(frozen=True)
class ResourceAmount:
    """Resources charged while queued, starting, running, or suspended."""

    memory_bytes: int
    disk_bytes: int = 0
    process_slots: int = 1
    start_slots: int = 1

    def __post_init__(self) -> None:
        if (
            min(
                self.memory_bytes,
                self.disk_bytes,
                self.process_slots,
                self.start_slots,
            )
            < 0
        ):
            raise ValueError("resource amounts cannot be negative")

    def __add__(self, other: ResourceAmount) -> ResourceAmount:
        return ResourceAmount(
            memory_bytes=self.memory_bytes + other.memory_bytes,
            disk_bytes=self.disk_bytes + other.disk_bytes,
            process_slots=self.process_slots + other.process_slots,
            start_slots=self.start_slots + other.start_slots,
        )

    def __sub__(self, other: ResourceAmount) -> ResourceAmount:
        return ResourceAmount(
            memory_bytes=self.memory_bytes - other.memory_bytes,
            disk_bytes=self.disk_bytes - other.disk_bytes,
            process_slots=self.process_slots - other.process_slots,
            start_slots=self.start_slots - other.start_slots,
        )

    def fits(self, limit: ResourceAmount) -> bool:
        """Return whether every resource axis fits within the limit."""
        return (
            self.memory_bytes <= limit.memory_bytes
            and self.disk_bytes <= limit.disk_bytes
            and self.process_slots <= limit.process_slots
            and self.start_slots <= limit.start_slots
        )


@dataclass(frozen=True)
class AdmissionEstimate:
    """Inputs to the normative maximum-based admission charge."""

    measured_p95_bytes: int
    declared_minimum_bytes: int
    runtime_floor_bytes: int
    profile_reservation_bytes: int
    backend_overhead_bytes: int
    concurrency: int = 1
    disk_bytes: int = 0
    declared_upper_bound_bytes: int | None = None

    def charge(self) -> ResourceAmount:
        """Calculate the admission charge without trusting a low declaration."""
        if self.concurrency <= 0:
            raise AdmissionError("admitted concurrency must be positive")
        base = max(
            self.measured_p95_bytes,
            self.declared_minimum_bytes,
            self.runtime_floor_bytes,
            self.profile_reservation_bytes,
            self.backend_overhead_bytes,
        )
        if self.declared_upper_bound_bytes is not None:
            base = max(base, self.declared_upper_bound_bytes)
        if base < 0 or self.disk_bytes < 0 or (
            self.declared_upper_bound_bytes is not None
            and self.declared_upper_bound_bytes < 0
        ):
            raise AdmissionError("admission estimates cannot be negative")
        return ResourceAmount(
            memory_bytes=base * self.concurrency,
            disk_bytes=self.disk_bytes,
            process_slots=self.concurrency,
            start_slots=1,
        )


@dataclass(frozen=True)
class ResourceReservation:
    """Opaque reservation owned by an accepted queue item or workload."""

    reservation_id: str
    profile_id: str
    amount: ResourceAmount


class ResourceLedger:
    """Atomic global/Profile accounting with a non-consumable Host guard."""

    def __init__(
        self,
        *,
        runtime_limit: ResourceAmount,
        host_free_guard: ResourceAmount,
        profile_limits: Mapping[str, ResourceAmount],
    ) -> None:
        self._runtime_limit = runtime_limit
        self._guard = host_free_guard
        self._profile_limits = dict(profile_limits)
        self._reservations: dict[str, ResourceReservation] = {}
        self._runtime_used = ResourceAmount(0, 0, 0, 0)
        self._profile_used: dict[str, ResourceAmount] = defaultdict(
            lambda: ResourceAmount(0, 0, 0, 0)
        )
        self._lock = RLock()

    def reserve(
        self,
        profile_id: str,
        amount: ResourceAmount,
    ) -> ResourceReservation:
        """Reserve resources or reject before materialization."""
        with self._lock:
            profile_limit = self._profile_limits.get(profile_id)
            if profile_limit is None:
                raise ResourceExhaustedError("Profile has no resource ceiling")
            runtime_effective = self._runtime_limit - self._guard
            next_runtime = self._runtime_used + amount
            next_profile = self._profile_used[profile_id] + amount
            if not next_runtime.fits(runtime_effective):
                raise ResourceExhaustedError(
                    "Host free-resource guard would be crossed"
                )
            if not next_profile.fits(profile_limit):
                raise ResourceExhaustedError("Profile resource ceiling exceeded")
            reservation = ResourceReservation(
                reservation_id=secrets.token_urlsafe(24),
                profile_id=profile_id,
                amount=amount,
            )
            self._reservations[reservation.reservation_id] = reservation
            self._runtime_used = next_runtime
            self._profile_used[profile_id] = next_profile
            return reservation

    def release(self, reservation_id: str) -> None:
        """Idempotently release a queue or workload reservation."""
        with self._lock:
            reservation = self._reservations.pop(reservation_id, None)
            if reservation is None:
                return
            self._runtime_used = self._runtime_used - reservation.amount
            self._profile_used[reservation.profile_id] = (
                self._profile_used[reservation.profile_id] - reservation.amount
            )

    @property
    def runtime_used(self) -> ResourceAmount:
        """Return an immutable current-use snapshot."""
        with self._lock:
            return self._runtime_used


def _strict_nonnegative_int(value: object) -> int:
    """Parse a persisted resource axis without coercing untrusted values."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("persisted resource values must be non-negative integers")
    return value


class DurableResourceLedger(ResourceLedger):
    """Persist short-lived reservations across a Host restart.

    The persisted record is deliberately a reservation ledger, not a caller
    supplied quota.  Every row is tied to the captured Profile/activation
    identity and expires if the owning Broker crashed before releasing it.
    Invalid or unreadable state fails closed so a damaged ledger cannot turn
    admission into an unbounded path.
    """

    _SCHEMA = "io.tobkiri.admission-ledger.v1"

    def __init__(
        self,
        *,
        runtime_limit: ResourceAmount,
        host_free_guard: ResourceAmount,
        profile_limits: Mapping[str, ResourceAmount],
        state_path: Path,
        identity: Mapping[str, str],
        lease_ttl_seconds: float = 120.0,
    ) -> None:
        if lease_ttl_seconds <= 0:
            raise ValueError("admission reservation TTL must be positive")
        super().__init__(
            runtime_limit=runtime_limit,
            host_free_guard=host_free_guard,
            profile_limits=profile_limits,
        )
        self._state_path = state_path
        self._identity = {str(key): str(value) for key, value in identity.items()}
        self._lease_ttl_seconds = lease_ttl_seconds
        self._expires_at: dict[str, float] = {}
        self._load_state()

    def reserve(
        self,
        profile_id: str,
        amount: ResourceAmount,
    ) -> ResourceReservation:
        """Reserve and durably journal resources before queue acceptance."""
        with self._lock:
            self._reap_expired_locked()
            reservation = super().reserve(profile_id, amount)
            self._expires_at[reservation.reservation_id] = (
                time.time() + self._lease_ttl_seconds
            )
            try:
                self._persist_locked()
            except Exception as exc:
                super().release(reservation.reservation_id)
                self._expires_at.pop(reservation.reservation_id, None)
                raise AdmissionError("admission reservation could not be journaled") from exc
            return reservation

    def release(self, reservation_id: str) -> None:
        """Release a reservation and persist the resulting ledger state."""
        with self._lock:
            super().release(reservation_id)
            self._expires_at.pop(reservation_id, None)
            try:
                self._persist_locked()
            except Exception as exc:
                raise AdmissionError("admission ledger could not be updated") from exc

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                raise ValueError("ledger root is not an object")
            if raw.get("schema") != self._SCHEMA:
                raise ValueError("ledger schema is not supported")
            if dict(raw.get("identity") or {}) != self._identity:
                # A Profile may reuse its workspace for a successor
                # activation.  Reservations from the old generation are not
                # admissible in the new one.
                return
            rows = raw.get("reservations")
            if not isinstance(rows, list):
                raise ValueError("ledger reservations are not an array")
            now = time.time()
            for row in rows:
                if not isinstance(row, Mapping):
                    raise ValueError("ledger reservation is not an object")
                reservation_id = str(row.get("reservation_id") or "")
                profile_id = str(row.get("profile_id") or "")
                expires_at = row.get("expires_at")
                if (
                    not reservation_id
                    or not profile_id
                    or isinstance(expires_at, bool)
                    or not isinstance(expires_at, (int, float))
                    or not math.isfinite(float(expires_at))
                    or expires_at <= now
                ):
                    continue
                amount_value = row.get("amount")
                if not isinstance(amount_value, Mapping):
                    raise ValueError("ledger reservation amount is not an object")
                amount = ResourceAmount(
                    memory_bytes=_strict_nonnegative_int(
                        amount_value["memory_bytes"]
                    ),
                    disk_bytes=_strict_nonnegative_int(
                        amount_value.get("disk_bytes", 0)
                    ),
                    process_slots=_strict_nonnegative_int(
                        amount_value.get("process_slots", 1)
                    ),
                    start_slots=_strict_nonnegative_int(
                        amount_value.get("start_slots", 1)
                    ),
                )
                if reservation_id in self._reservations:
                    raise ValueError("ledger contains duplicate reservation IDs")
                if profile_id not in self._profile_limits:
                    raise ValueError("ledger reservation targets an unknown Profile")
                reservation = ResourceReservation(
                    reservation_id=reservation_id,
                    profile_id=profile_id,
                    amount=amount,
                )
                self._reservations[reservation_id] = reservation
                self._expires_at[reservation_id] = float(expires_at)
                self._runtime_used = self._runtime_used + amount
                self._profile_used[profile_id] = self._profile_used[profile_id] + amount
            if not self._runtime_used.fits(self._runtime_limit - self._guard):
                raise ValueError("ledger exceeds the Host runtime ceiling")
            for profile_id, used in self._profile_used.items():
                if not used.fits(self._profile_limits[profile_id]):
                    raise ValueError("ledger exceeds the Profile resource ceiling")
        except (
            OSError,
            OverflowError,
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
        ) as exc:
            raise AdmissionError("durable admission ledger is invalid") from exc

    def _reap_expired_locked(self) -> None:
        now = time.time()
        for reservation_id, expires_at in tuple(self._expires_at.items()):
            if expires_at <= now:
                super().release(reservation_id)
                self._expires_at.pop(reservation_id, None)

    def _persist_locked(self) -> None:
        rows = [
            {
                "reservation_id": reservation.reservation_id,
                "profile_id": reservation.profile_id,
                "amount": {
                    "memory_bytes": reservation.amount.memory_bytes,
                    "disk_bytes": reservation.amount.disk_bytes,
                    "process_slots": reservation.amount.process_slots,
                    "start_slots": reservation.amount.start_slots,
                },
                "expires_at": self._expires_at[reservation.reservation_id],
            }
            for reservation in sorted(
                self._reservations.values(),
                key=lambda item: item.reservation_id,
            )
        ]
        payload = {
            "schema": self._SCHEMA,
            "identity": self._identity,
            "reservations": rows,
        }
        self._state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self._state_path.with_name(
            f".{self._state_path.name}.{secrets.token_hex(8)}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._state_path)
        finally:
            if temporary.exists():
                temporary.unlink()


@dataclass(frozen=True)
class QueueScope:
    """All fairness and quota dimensions for one request."""

    profile_id: str
    caller_id: str
    pack_id: str
    binding_id: str
    priority: str = "foreground"


@dataclass(frozen=True)
class QueueItem:
    """Accepted item which exclusively owns its resource reservation."""

    item_id: str
    scope: QueueScope
    reservation: ResourceReservation
    enqueued_at: float
    deadline: float


class FairAdmissionQueue:
    """Bounded round-robin queue across binding and broader scopes."""

    def __init__(
        self,
        ledger: ResourceLedger,
        *,
        global_limit: int = 256,
        profile_limit: int = 64,
        background_limit: int = 128,
        caller_limit: int = 32,
        pack_limit: int = 32,
        binding_limit: int = 16,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ledger = ledger
        self._limits = {
            "global": global_limit,
            "profile": profile_limit,
            "background": background_limit,
            "caller": caller_limit,
            "pack": pack_limit,
            "binding": binding_limit,
        }
        if any(limit <= 0 for limit in self._limits.values()):
            raise ValueError("queue limits must be positive")
        self._clock = clock
        self._items: dict[str, QueueItem] = {}
        self._by_binding: dict[str, deque[str]] = defaultdict(deque)
        self._binding_round_robin: deque[str] = deque()
        self._counts: Counter[tuple[str, str]] = Counter()
        self._lock = RLock()

    def enqueue(
        self,
        scope: QueueScope,
        amount: ResourceAmount,
        *,
        wait_timeout_seconds: float,
    ) -> QueueItem:
        """Atomically check all bounds and reserve before accepting an item."""
        if wait_timeout_seconds <= 0:
            raise AdmissionError("queue wait timeout must be positive")
        with self._lock:
            self._check_bounds(scope)
            reservation = self._ledger.reserve(scope.profile_id, amount)
            now = self._clock()
            item = QueueItem(
                item_id=secrets.token_urlsafe(24),
                scope=scope,
                reservation=reservation,
                enqueued_at=now,
                deadline=now + wait_timeout_seconds,
            )
            self._items[item.item_id] = item
            queue = self._by_binding[scope.binding_id]
            if not queue:
                self._binding_round_robin.append(scope.binding_id)
            queue.append(item.item_id)
            self._increment(scope, 1)
            return item

    def admit(
        self,
        scope: QueueScope,
        amount: ResourceAmount,
        *,
        wait_timeout_seconds: float,
    ) -> ResourceReservation:
        """Reserve one request without exposing a racy intermediate queue pop.

        Production admission has already bounded the Broker deadline when it
        calls this method.  Keeping enqueue and pop under the same queue lock
        preserves the existing fairness implementation for callers that use
        the queue directly while making the accepted reservation atomic for a
        request that immediately enters execution.
        """

        with self._lock:
            item = self.enqueue(
                scope,
                amount,
                wait_timeout_seconds=wait_timeout_seconds,
            )
            queue = self._by_binding[item.scope.binding_id]
            queue.remove(item.item_id)
            if not queue:
                self._by_binding.pop(item.scope.binding_id, None)
                self._binding_round_robin = deque(
                    binding
                    for binding in self._binding_round_robin
                    if binding != item.scope.binding_id
                )
            self._items.pop(item.item_id, None)
            self._increment(item.scope, -1)
            if self._clock() >= item.deadline:
                self._ledger.release(item.reservation.reservation_id)
                raise TimeoutError("admission deadline expired")
            return item.reservation

    def pop(self) -> QueueItem | None:
        """Pop fairly across bindings, releasing expired reservations."""
        with self._lock:
            attempts = len(self._binding_round_robin)
            while attempts:
                binding_id = self._binding_round_robin.popleft()
                queue = self._by_binding[binding_id]
                item_id = queue.popleft()
                if queue:
                    self._binding_round_robin.append(binding_id)
                else:
                    self._by_binding.pop(binding_id, None)
                item = self._items.pop(item_id)
                self._increment(item.scope, -1)
                if self._clock() >= item.deadline:
                    self._ledger.release(item.reservation.reservation_id)
                    attempts -= 1
                    continue
                return item
            return None

    def cancel(self, item_id: str) -> None:
        """Cancel a queued item and immediately release its reservation."""
        with self._lock:
            item = self._items.pop(item_id, None)
            if item is None:
                return
            queue = self._by_binding[item.scope.binding_id]
            queue.remove(item_id)
            if not queue:
                self._by_binding.pop(item.scope.binding_id, None)
                self._binding_round_robin = deque(
                    binding
                    for binding in self._binding_round_robin
                    if binding != item.scope.binding_id
                )
            self._increment(item.scope, -1)
            self._ledger.release(item.reservation.reservation_id)

    def complete(self, item: QueueItem) -> None:
        """Release the reservation when the workload no longer owns resources."""
        self._ledger.release(item.reservation.reservation_id)

    def _check_bounds(self, scope: QueueScope) -> None:
        checks = [
            (("global", "*"), self._limits["global"]),
            (("profile", scope.profile_id), self._limits["profile"]),
            (("caller", scope.caller_id), self._limits["caller"]),
            (("pack", scope.pack_id), self._limits["pack"]),
            (("binding", scope.binding_id), self._limits["binding"]),
        ]
        if scope.priority == "background":
            checks.append((("background", "*"), self._limits["background"]))
        for key, limit in checks:
            if self._counts[key] >= limit:
                raise QueueFullError(f"queue limit reached for {key[0]}")

    def _increment(self, scope: QueueScope, delta: int) -> None:
        keys = [
            ("global", "*"),
            ("profile", scope.profile_id),
            ("caller", scope.caller_id),
            ("pack", scope.pack_id),
            ("binding", scope.binding_id),
        ]
        if scope.priority == "background":
            keys.append(("background", "*"))
        for key in keys:
            self._counts[key] += delta
            if self._counts[key] == 0:
                del self._counts[key]
