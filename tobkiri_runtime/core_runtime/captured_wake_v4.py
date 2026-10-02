"""Generic finite Host process wake registrations and fresh request delivery.

Wake leases authorize only the private wake gate. They are never execution
leases and never enter the Broker. Every delivery uses a fresh Host context
and the normal Broker authorization/audit path supplied by production capture.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from typing import Any, Callable, Mapping, Protocol

from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_host.ports import OpaqueInvocationLease
from tobkiri_host.triggers import (
    OSWakeAdapter,
    TriggerDelivery,
    TriggerRegistration,
    TriggerWakeKernel,
    WakeAdapterStatus,
    WakeRegistrationLease,
)
from tobkiri_protocol.canonical import canonical_digest


@dataclass(frozen=True)
class CapturedWakeDeclarationV4:
    """Verified factory declaration for one exact fixed-payload target."""

    contract_id: str
    operation_id: str
    payload: Mapping[str, Any]
    interval_ms: int

    def __post_init__(self) -> None:
        if not self.contract_id or not self.operation_id:
            raise ValueError("wake target is missing")
        if type(self.interval_ms) is not int or not 100 <= self.interval_ms <= 86400000:
            raise ValueError("wake interval is invalid")
        canonical_digest(dict(self.payload))


class CapturedWakePortV4(Protocol):
    """Narrow Pack port; no session, grant, Broker, or arbitrary target input."""

    def arm(self, invocation: Any, duration_ms: int) -> Mapping[str, Any]:
        """Arm the one captured target for a finite approved duration."""

    def disarm(self, invocation: Any) -> Mapping[str, Any]:
        """Fence and disable this exact registration."""

    def status(self) -> Mapping[str, Any]:
        """Return truthful driver/adapter and finite registration status."""


class LateBoundWakePortV4:
    """Bind one verified factory after the active capture's Broker exists."""

    def __init__(self) -> None:
        self._driver: CapturedWakeDriverV4 | None = None
        self._owner: str | None = None

    def bind(self, driver: CapturedWakeDriverV4, owner_principal_id: str) -> None:
        """Install exactly one Host-owned driver and exact owner identity."""
        if self._driver is not None or not owner_principal_id:
            raise PermissionError("wake port is already bound or invalid")
        self._driver, self._owner = driver, owner_principal_id

    def _current(self, invocation: Any) -> CapturedWakeDriverV4:
        invocation.assert_current()
        if self._driver is None or invocation.envelope.target_principal.value != self._owner:
            raise PermissionError("captured wake owner is unavailable")
        return self._driver

    def arm(self, invocation: Any, duration_ms: int) -> Mapping[str, Any]:
        """Arm only after authenticating the current exact owner invocation."""
        return self._current(invocation).arm(duration_ms)

    def disarm(self, invocation: Any) -> Mapping[str, Any]:
        """Revoke only the authenticated owner's finite registration."""
        return self._current(invocation).disarm()

    def status(self) -> Mapping[str, Any]:
        """Never claim an unbound production driver is armed."""
        return (
            self._driver.status()
            if self._driver
            else {
                "armed": False,
                "available": False,
                "reason": "wake_driver_unavailable",
            }
        )


class HostProcessWakeAdapterV4:
    """Explicit Host timer; runs while this process lives, never wakes a Mac."""

    def __init__(self, receive: Callable[[], None]) -> None:
        self.status = WakeAdapterStatus("host-process-timer.v1", "host_process_only", True)
        self._receive = receive
        self._condition = threading.Condition()
        self._leases: dict[str, WakeRegistrationLease] = {}
        self._due: dict[tuple[str, str], float] = {}
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="tobkiri-host-wake", daemon=True)
        self._thread.start()

    def register(self, registration: TriggerRegistration) -> WakeRegistrationLease:
        """Register one process-owned source with its own opaque finite lease."""
        with self._condition:
            if self._closed:
                raise PermissionError("wake adapter is closed")
            lease = WakeRegistrationLease(
                secrets.token_hex(16), registration.registration_id, registration.security_epoch
            )
            self._leases[registration.registration_id] = lease
            return lease

    def arm(self, lease: WakeRegistrationLease, occurrence_id: str, due_monotonic: float) -> None:
        """Schedule one exact occurrence; no Provider payload enters the timer."""
        with self._condition:
            if self._closed or self._leases.get(lease.registration_id) != lease:
                raise PermissionError("wake registration lease is stale")
            self._due[(lease.registration_id, occurrence_id)] = due_monotonic
            self._condition.notify_all()

    def revoke(self, lease: WakeRegistrationLease) -> None:
        """Fence all occurrences owned by this exact registration lease."""
        with self._condition:
            if self._leases.get(lease.registration_id) == lease:
                self._leases.pop(lease.registration_id)
                self._due = {
                    key: value
                    for key, value in self._due.items()
                    if key[0] != lease.registration_id
                }
                self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._closed:
                    return
                now = time.monotonic()
                ready = [key for key, value in self._due.items() if value <= now]
                if not ready:
                    delay = min(self._due.values()) - now if self._due else 60.0
                    self._condition.wait(max(0.001, min(60.0, delay)))
                    continue
                for key in ready:
                    self._due.pop(key, None)
            try:
                self._receive()
            except Exception:
                # Driver persists a sanitized unavailable reason; the timer
                # never retries a failing callback without a newly armed gate.
                continue

    def close(self) -> None:
        """Stop this process source without deleting durable registration intent."""
        with self._condition:
            self._closed = True
            self._leases.clear()
            self._due.clear()
            self._condition.notify_all()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)


class _OccurrenceGate:
    """Host-private one-shot wake gate, separate from execution authority."""

    def __init__(self, check: Callable[[], None]) -> None:
        self.check = check
        self.tokens: set[bytes] = set()

    def issue_trigger_lease(
        self,
        registration_id: str,
        occurrence_id: str,
        target: OpaqueAuthorityRef,
        security_epoch: int,
    ) -> OpaqueInvocationLease:
        self.check()
        token = secrets.token_bytes(32)
        self.tokens.add(token)
        return OpaqueInvocationLease(token)

    def consume(self, delivery: TriggerDelivery) -> None:
        if delivery.lease.token not in self.tokens:
            raise PermissionError("wake gate is unknown or already consumed")
        self.tokens.remove(delivery.lease.token)


class CapturedWakeDriverV4:
    """Durable finite registration, restart reconstruction and fresh delivery."""

    def __init__(
        self,
        *,
        state_path: Path,
        declaration: CapturedWakeDeclarationV4,
        registration: TriggerRegistration,
        binding_identity: Mapping[str, Any],
        assert_authorized: Callable[[], None],
        invoke_fresh: Callable[[str, Callable[[], None]], Mapping[str, Any]],
        current_epoch: Callable[[], int],
        adapter_factory: Callable[[Callable[[], None]], OSWakeAdapter] | None = None,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._declaration, self._registration = declaration, registration
        self._identity = canonical_digest(dict(binding_identity))
        self._check, self._invoke = assert_authorized, invoke_fresh
        self._wall, self._mono = wall_clock, monotonic_clock
        self._lock = threading.RLock()
        self._closed = False
        self._armed = False
        self._reason = "registration_disabled"
        state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if state_path.is_symlink():
            raise PermissionError("wake state path is invalid")
        self._database = sqlite3.connect(state_path, check_same_thread=False)
        state_path.chmod(0o600)
        self._database.execute("PRAGMA journal_mode=WAL")
        self._database.execute(
            "CREATE TABLE IF NOT EXISTS wake_state (id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL, generation INTEGER NOT NULL, enabled INTEGER NOT NULL, expires_wall REAL NOT NULL, next_wall REAL NOT NULL, claim_until REAL NOT NULL DEFAULT 0)"
        )
        self._database.commit()
        self._gate = _OccurrenceGate(self._check)
        self._adapter = adapter_factory(self.deliver_due) if adapter_factory else None
        self._kernel_db = sqlite3.connect(":memory:", check_same_thread=False)
        self._kernel = TriggerWakeKernel(
            self._kernel_db,
            self._gate,
            clock=self._mono,
            wake_adapter=self._adapter,
            current_security_epoch=current_epoch,
            production=True,
        )
        self._registered = False
        self._active_registration = registration
        self._restore()

    def _state(self) -> tuple[Any, ...] | None:
        return self._database.execute(
            "SELECT identity,generation,enabled,expires_wall,next_wall,claim_until FROM wake_state WHERE id=1"
        ).fetchone()

    def _restore(self) -> None:
        with self._lock:
            state = self._state()
            if not state or not state[2]:
                return
            if state[0] != self._identity or state[3] <= self._wall():
                self._reason = "registration_expired_or_binding_changed"
                return
            try:
                self._check()
                self._arm_kernel(state)
            except Exception:
                self._reason = "wake_authority_or_adapter_unavailable"

    def _arm_kernel(self, state: tuple[Any, ...]) -> None:
        if self._closed:
            raise PermissionError("wake driver is closed")
        if not self._registered:
            self._active_registration = replace(
                self._registration,
                registration_id=f"{self._registration.registration_id}.g{state[1]}",
            )
            self._kernel.register(self._active_registration)
            self._registered = True
        occurrence = f"{state[1]}:{int(state[4] * 1000)}"
        due = self._mono() + max(0.0, state[4] - self._wall())
        self._kernel.schedule(self._active_registration.registration_id, occurrence, due)
        self._armed, self._reason = True, ""

    def arm(self, duration_ms: int) -> Mapping[str, Any]:
        """Persist intent only after exact current authority and source admission."""
        if type(duration_ms) is not int or not 1000 <= duration_ms <= 86400000:
            raise ValueError("finite wake duration is invalid")
        with self._lock:
            self._check()
            if self._adapter is None or not self._adapter.status.available:
                raise PermissionError("Host wake adapter is unavailable")
            state = self._state()
            generation = int(state[1]) + 1 if state else 1
            if self._registered:
                self._kernel.revoke(self._active_registration.registration_id)
                self._registered = False
            now = self._wall()
            with self._database:
                self._database.execute(
                    "INSERT OR REPLACE INTO wake_state VALUES (1,?,?,?,?,?,0)",
                    (
                        self._identity,
                        generation,
                        1,
                        now + duration_ms / 1000,
                        now + self._declaration.interval_ms / 1000,
                    ),
                )
            try:
                self._arm_kernel(self._state())  # type: ignore[arg-type]
            except Exception:
                self.disarm()
                raise
            return self.status()

    def disarm(self) -> Mapping[str, Any]:
        """Fence pending/delivering callbacks before revoking the timer source."""
        with self._lock, self._database:
            self._database.execute(
                "UPDATE wake_state SET enabled=0,generation=generation+1 WHERE id=1"
            )
            if self._registered:
                self._kernel.revoke(self._active_registration.registration_id)
                self._registered = False
            self._armed, self._reason = False, "registration_disabled"
            return self.status()

    def deliver_due(self) -> None:
        """Claim one due gate then execute a freshly scoped audited request."""
        with self._lock:
            if self._closed or not self._armed:
                return
            state = self._state()
            if not state or not state[2] or state[3] <= self._wall():
                self.disarm()
                self._reason = "registration_expired"
                return
            try:
                delivery = self._kernel.claim_due()
                if delivery is None:
                    return
                self._gate.consume(delivery)
                generation, due_wall = state[1], state[4]
                if delivery.occurrence_id != f"{generation}:{int(due_wall * 1000)}":
                    self._kernel.acknowledge(delivery)
                    return
                with self._database:
                    claimed = self._database.execute(
                        "UPDATE wake_state SET claim_until=? WHERE id=1 AND generation=? AND enabled=1 AND identity=? AND next_wall=? AND claim_until<=?",
                        (self._wall() + 30, generation, self._identity, due_wall, self._wall()),
                    ).rowcount
                if claimed != 1:
                    self._armed, self._reason = False, "wake_claim_owned_elsewhere"
                    return

                def fence() -> None:
                    current = self._state()
                    if (
                        self._closed
                        or not current
                        or current[0] != self._identity
                        or current[1] != generation
                        or not current[2]
                        or current[3] <= self._wall()
                    ):
                        raise PermissionError("wake registration is fenced")
                    self._check()

                fence()
            except Exception:
                self.disarm()
                self._reason = "wake_authority_or_adapter_unavailable"
                return
        try:
            result = self._invoke(delivery.occurrence_id, fence)
            if result.get("status") in {
                "failed",
                "error",
                "denied",
                "approval_required",
                "unavailable",
            }:
                raise PermissionError("wake execution did not complete")
            with self._lock:
                fence()
                self._kernel.acknowledge(delivery)
                next_wall = max(
                    due_wall + self._declaration.interval_ms / 1000,
                    self._wall() + self._declaration.interval_ms / 1000,
                )
                with self._database:
                    self._database.execute(
                        "UPDATE wake_state SET next_wall=?,claim_until=0 WHERE id=1 AND generation=? AND enabled=1",
                        (next_wall, generation),
                    )
                self._arm_kernel(self._state())  # type: ignore[arg-type]
        except Exception:
            with self._lock:
                self.disarm()
                self._reason = "wake_execution_unavailable"

    def status(self) -> Mapping[str, Any]:
        """Armed requires an existing driver, source lease and finite intent."""
        with self._lock:
            state = self._state()
            return {
                "armed": self._armed
                and not self._closed
                and bool(state and state[2] and state[3] > self._wall()),
                "available": not self._closed
                and bool(self._adapter and self._adapter.status.available),
                "wake_scope": "host_process_only",
                "reason": self._reason,
                "expires_at_ms": int(state[3] * 1000) if state else None,
                "next_wake_at_ms": int(state[4] * 1000) if state else None,
            }

    def close(self) -> None:
        """Fence this capture; a restart must independently recheck intent/grant."""
        with self._lock:
            self._closed, self._armed = True, False
            if self._registered:
                self._kernel.revoke(self._active_registration.registration_id)
            self._registered = False
        close = getattr(self._adapter, "close", None)
        if callable(close):
            close()
