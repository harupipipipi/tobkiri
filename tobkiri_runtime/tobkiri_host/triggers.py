"""Minimal durable Trigger/Wake Kernel without scheduler semantics."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from threading import RLock
import time
from typing import Callable, Protocol

from .errors import TriggerError
from .models import OpaqueAuthorityRef
from .ports import OpaqueInvocationLease


class TriggerAuthorityPort(Protocol):
    """Occurrence-bound authority surface needed by the wake kernel."""

    def issue_trigger_lease(
        self,
        registration_id: str,
        occurrence_id: str,
        target: OpaqueAuthorityRef,
        security_epoch: int,
    ) -> OpaqueInvocationLease:
        """Issue a current-epoch one-shot occurrence lease."""


@dataclass(frozen=True)
class TriggerRegistration:
    """Pinned trigger Contract target; recurrence meaning remains outside Kernel."""

    registration_id: str
    contract_id: str
    operation_id: str
    target: OpaqueAuthorityRef
    activation_digest: str
    security_epoch: int
    pending_quota: int = 16


@dataclass(frozen=True)
class TriggerDelivery:
    """Claimed occurrence and its trigger-specific one-shot lease."""

    registration: TriggerRegistration
    occurrence_id: str
    attempt: int
    lease: OpaqueInvocationLease


@dataclass(frozen=True)
class WakeAdapterStatus:
    """Deterministic OS wake adapter capability state."""

    adapter_id: str
    platform: str
    available: bool
    reason: str | None = None


@dataclass(frozen=True)
class WakeRegistrationLease:
    """Opaque finite binding between one registration and an OS wake source."""

    lease_id: str
    registration_id: str
    security_epoch: int


class OSWakeAdapter(Protocol):
    """Host-owned OS wake integration; it is never discovered from env/daemon."""

    status: WakeAdapterStatus

    def register(self, registration: TriggerRegistration) -> WakeRegistrationLease:
        """Register one exact wake target and return its Host-owned lease."""

    def arm(
        self,
        lease: WakeRegistrationLease,
        occurrence_id: str,
        due_monotonic: float,
    ) -> None:
        """Arm one deduplicated occurrence."""

    def revoke(self, lease: WakeRegistrationLease) -> None:
        """Revoke one registration and all pending OS wake state."""


class TriggerWakeKernel:
    """Durable bounded at-least-once occurrence claimant.

    The Scheduler computes calendar/recurrence/timezone meaning and submits exact
    occurrence IDs and monotonic due times. This Kernel never runs Scheduler code.
    """

    def __init__(
        self,
        database: sqlite3.Connection,
        authority: TriggerAuthorityPort,
        *,
        clock: Callable[[], float] = time.monotonic,
        claim_timeout_seconds: float = 30.0,
        wake_adapter: OSWakeAdapter | None = None,
        current_security_epoch: Callable[[], int] | None = None,
        production: bool = False,
    ) -> None:
        self._database = database
        self._authority = authority
        self._clock = clock
        self._claim_timeout = claim_timeout_seconds
        self._wake_adapter = wake_adapter
        self._current_security_epoch = current_security_epoch
        self._production = production
        self._wake_leases: dict[str, WakeRegistrationLease] = {}
        self._lock = RLock()
        self._database.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self._database.executescript(
            """
            CREATE TABLE IF NOT EXISTS trigger_registration (
                registration_id TEXT PRIMARY KEY,
                contract_id TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                target_ref TEXT NOT NULL,
                activation_digest TEXT NOT NULL,
                security_epoch INTEGER NOT NULL,
                pending_quota INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS trigger_occurrence (
                registration_id TEXT NOT NULL,
                occurrence_id TEXT NOT NULL,
                due_monotonic REAL NOT NULL,
                status TEXT NOT NULL,
                claimed_at REAL,
                attempt INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (registration_id, occurrence_id),
                FOREIGN KEY (registration_id)
                    REFERENCES trigger_registration(registration_id)
                    ON DELETE CASCADE
            );
            """
        )
        self._database.commit()

    def register(self, registration: TriggerRegistration) -> None:
        """Durably register an exact pinned Trigger Contract target."""
        if registration.pending_quota <= 0:
            raise TriggerError("pending quota must be positive")
        if self._production:
            self._require_current_epoch(registration.security_epoch)
            if self._wake_adapter is None or not self._wake_adapter.status.available:
                reason = (
                    self._wake_adapter.status.reason
                    if self._wake_adapter is not None
                    else "OS wake adapter is not registered"
                )
                raise TriggerError(reason or "OS wake adapter is unavailable")
            lease = self._wake_adapter.register(registration)
            if (
                lease.registration_id != registration.registration_id
                or lease.security_epoch != registration.security_epoch
                or not lease.lease_id
            ):
                raise TriggerError("OS wake registration lease mismatch")
            self._wake_leases[registration.registration_id] = lease
        with self._lock, self._database:
            self._database.execute(
                """
                INSERT INTO trigger_registration (
                    registration_id, contract_id, operation_id, target_ref,
                    activation_digest, security_epoch, pending_quota, enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    registration.registration_id,
                    registration.contract_id,
                    registration.operation_id,
                    registration.target.value,
                    registration.activation_digest,
                    registration.security_epoch,
                    registration.pending_quota,
                ),
            )

    def schedule(
        self,
        registration_id: str,
        occurrence_id: str,
        due_monotonic: float,
    ) -> bool:
        """Store one deduplicated occurrence subject to registration quota."""
        wake_lease: WakeRegistrationLease | None = None
        with self._lock, self._database:
            row = self._database.execute(
                """
                SELECT pending_quota, enabled, security_epoch
                FROM trigger_registration WHERE registration_id = ?
                """,
                (registration_id,),
            ).fetchone()
            if row is None or not row[1]:
                raise TriggerError("trigger is unknown or disabled")
            registration_epoch = int(row[2])
            if self._production:
                self._require_current_epoch(registration_epoch)
                wake_lease = self._wake_leases.get(registration_id)
                if wake_lease is None or self._wake_adapter is None:
                    raise TriggerError("OS wake registration lease is unavailable")
            duplicate = self._database.execute(
                """
                SELECT 1 FROM trigger_occurrence
                WHERE registration_id = ? AND occurrence_id = ?
                """,
                (registration_id, occurrence_id),
            ).fetchone()
            if duplicate is not None:
                return False
            pending = self._database.execute(
                """
                SELECT COUNT(*) FROM trigger_occurrence
                WHERE registration_id = ? AND status != 'delivered'
                """,
                (registration_id,),
            ).fetchone()[0]
            if pending >= row[0]:
                raise TriggerError("trigger pending quota exceeded")
            cursor = self._database.execute(
                """
                INSERT OR IGNORE INTO trigger_occurrence (
                    registration_id, occurrence_id, due_monotonic, status
                ) VALUES (?, ?, ?, 'pending')
                """,
                (registration_id, occurrence_id, due_monotonic),
            )
            inserted = cursor.rowcount == 1
            if inserted and self._production:
                if wake_lease is None or self._wake_adapter is None:
                    raise TriggerError("OS wake registration lease is unavailable")
                self._wake_adapter.arm(
                    wake_lease,
                    occurrence_id,
                    due_monotonic,
                )
        return inserted

    def claim_due(self) -> TriggerDelivery | None:
        """Claim one due occurrence and issue its one-shot authority lease."""
        now = self._clock()
        with self._lock, self._database:
            self._database.execute(
                """
                UPDATE trigger_occurrence
                SET status = 'pending', claimed_at = NULL
                WHERE status = 'claimed' AND claimed_at < ?
                """,
                (now - self._claim_timeout,),
            )
            row = self._database.execute(
                """
                SELECT r.registration_id, r.contract_id, r.operation_id,
                       r.target_ref, r.activation_digest, r.security_epoch,
                       r.pending_quota, o.occurrence_id, o.attempt
                FROM trigger_occurrence o
                JOIN trigger_registration r USING (registration_id)
                WHERE o.status = 'pending' AND o.due_monotonic <= ?
                  AND r.enabled = 1
                ORDER BY o.due_monotonic, r.registration_id, o.occurrence_id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            updated = self._database.execute(
                """
                UPDATE trigger_occurrence
                SET status = 'claimed', claimed_at = ?, attempt = attempt + 1
                WHERE registration_id = ? AND occurrence_id = ?
                  AND status = 'pending'
                """,
                (now, row[0], row[7]),
            )
            if updated.rowcount != 1:
                return None
        registration = TriggerRegistration(
            registration_id=row[0],
            contract_id=row[1],
            operation_id=row[2],
            target=OpaqueAuthorityRef(row[3]),
            activation_digest=row[4],
            security_epoch=row[5],
            pending_quota=row[6],
        )
        try:
            if self._production:
                self._require_current_epoch(registration.security_epoch)
                wake_lease = self._wake_leases.get(registration.registration_id)
                if wake_lease is None:
                    raise TriggerError("wake without a registration lease")
            lease = self._authority.issue_trigger_lease(
                registration.registration_id,
                row[7],
                registration.target,
                registration.security_epoch,
            )
        except Exception:
            with self._lock, self._database:
                self._database.execute(
                    """
                    UPDATE trigger_occurrence
                    SET status = 'pending', claimed_at = NULL
                    WHERE registration_id = ? AND occurrence_id = ?
                    """,
                    (registration.registration_id, row[7]),
                )
            raise
        return TriggerDelivery(
            registration=registration,
            occurrence_id=row[7],
            attempt=row[8] + 1,
            lease=lease,
        )

    def revoke(self, registration_id: str) -> None:
        """Disable a trigger and revoke its OS wake registration lease."""
        with self._lock, self._database:
            updated = self._database.execute(
                "UPDATE trigger_registration SET enabled = 0 WHERE registration_id = ?",
                (registration_id,),
            )
            if updated.rowcount != 1:
                raise TriggerError("trigger is unknown")
        wake_lease = self._wake_leases.pop(registration_id, None)
        if wake_lease is not None and self._wake_adapter is not None:
            self._wake_adapter.revoke(wake_lease)

    def _require_current_epoch(self, expected: int) -> None:
        if self._current_security_epoch is None:
            raise TriggerError("SecurityEpoch source is unavailable")
        if self._current_security_epoch() != expected:
            raise TriggerError("trigger SecurityEpoch is stale")

    def acknowledge(self, delivery: TriggerDelivery) -> None:
        """Mark one claimed occurrence delivered; missing claims fail closed."""
        with self._lock, self._database:
            cursor = self._database.execute(
                """
                UPDATE trigger_occurrence SET status = 'delivered'
                WHERE registration_id = ? AND occurrence_id = ?
                  AND status = 'claimed'
                """,
                (
                    delivery.registration.registration_id,
                    delivery.occurrence_id,
                ),
            )
            if cursor.rowcount != 1:
                raise TriggerError("trigger occurrence is not currently claimed")


__all__ = [
    "OSWakeAdapter",
    "TriggerDelivery",
    "TriggerRegistration",
    "TriggerWakeKernel",
    "WakeAdapterStatus",
    "WakeRegistrationLease",
]
