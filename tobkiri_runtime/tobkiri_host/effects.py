"""External-effect uncertainty and explicit reconciliation records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import secrets
from threading import RLock
import time
from typing import Any, Mapping, Protocol

from .errors import AmbiguousEffectError


class EffectDisposition(str, Enum):
    """What the Host can prove about external acceptance."""

    NOT_ACCEPTED = "not_accepted"
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderOutcome:
    """Sanitized provider result plus external-effect disposition."""

    payload: Mapping[str, Any] | None
    disposition: EffectDisposition = EffectDisposition.COMPLETED
    receipt: str | None = None


@dataclass(frozen=True)
class ReconciliationRecord:
    """Durable-store-shaped record for an ambiguous external effect."""

    reconciliation_id: str
    request_id: str
    target_ref: str
    idempotency_key: str | None
    reconcile_operation: str | None
    created_at: float
    status: str = "needs_reconciliation"
    receipt: str | None = None


class ReconciliationStore(Protocol):
    """Persistence interface for effects that cannot be safely retried."""

    def create(
        self,
        *,
        request_id: str,
        target_ref: str,
        idempotency_key: str | None,
        reconcile_operation: str | None,
        receipt: str | None,
    ) -> ReconciliationRecord:
        """Persist uncertainty before returning ``ambiguous_effect``."""

    def resolve(
        self,
        reconciliation_id: str,
        disposition: EffectDisposition,
        receipt: str | None,
    ) -> ReconciliationRecord:
        """Resolve using a Contract-defined receipt/status procedure."""


class InMemoryReconciliationStore:
    """Thread-safe reference store; production can replace it with a journal."""

    def __init__(self) -> None:
        self._records: dict[str, ReconciliationRecord] = {}
        self._lock = RLock()

    def create(
        self,
        *,
        request_id: str,
        target_ref: str,
        idempotency_key: str | None,
        reconcile_operation: str | None,
        receipt: str | None,
    ) -> ReconciliationRecord:
        """Persist a new unresolved effect."""
        record = ReconciliationRecord(
            reconciliation_id=secrets.token_urlsafe(24),
            request_id=request_id,
            target_ref=target_ref,
            idempotency_key=idempotency_key,
            reconcile_operation=reconcile_operation,
            created_at=time.time(),
            receipt=receipt,
        )
        with self._lock:
            self._records[record.reconciliation_id] = record
        return record

    def resolve(
        self,
        reconciliation_id: str,
        disposition: EffectDisposition,
        receipt: str | None,
    ) -> ReconciliationRecord:
        """Mark a record resolved only when absence or completion is proven."""
        if disposition not in {
            EffectDisposition.NOT_ACCEPTED,
            EffectDisposition.COMPLETED,
        }:
            raise AmbiguousEffectError(reconciliation_id)
        with self._lock:
            current = self._records[reconciliation_id]
            record = ReconciliationRecord(
                reconciliation_id=current.reconciliation_id,
                request_id=current.request_id,
                target_ref=current.target_ref,
                idempotency_key=current.idempotency_key,
                reconcile_operation=current.reconcile_operation,
                created_at=current.created_at,
                status=(
                    "not_accepted"
                    if disposition is EffectDisposition.NOT_ACCEPTED
                    else "completed"
                ),
                receipt=receipt or current.receipt,
            )
            self._records[reconciliation_id] = record
            return record

    def get(self, reconciliation_id: str) -> ReconciliationRecord:
        """Return one record for a reconciliation worker or status API."""
        with self._lock:
            return self._records[reconciliation_id]


def raise_ambiguous(
    store: ReconciliationStore,
    *,
    request_id: str,
    target_ref: str,
    idempotency_key: str | None,
    reconcile_operation: str | None,
    receipt: str | None = None,
) -> None:
    """Persist uncertainty and raise a stable non-retryable error."""
    record = store.create(
        request_id=request_id,
        target_ref=target_ref,
        idempotency_key=idempotency_key,
        reconcile_operation=reconcile_operation,
        receipt=receipt,
    )
    raise AmbiguousEffectError(record.reconciliation_id)
