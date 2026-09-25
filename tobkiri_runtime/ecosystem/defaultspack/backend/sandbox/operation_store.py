from __future__ import annotations

import builtins
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
import threading
from typing import Any, Callable, TypeVar

from .locked_json_store import LockedJsonStore


TERMINAL_OPERATION_STATES = {"completed", "failed", "cancelled"}
_T = TypeVar("_T")


class RuntimeOperationConflict(ValueError):
    """Raised when an idempotency key is reused for different work."""


class RuntimeOperationLeaseLost(RuntimeError):
    """Raised when a stale worker attempts to mutate an operation."""


class RuntimeOperationStore:
    """Transactionally persist and coordinate managed-runtime operations."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._memory: dict[str, Any] = {"operations": {}}
        self._store = LockedJsonStore(self.path) if self.path is not None else None
        self._execution_lock = threading.RLock()

    def list(self) -> list[dict[str, Any]]:
        """Return raw durable records, newest first."""
        operations = self._read_operations()
        return sorted(
            (dict(item) for item in operations.values()),
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )

    def list_projected(self, *, now: str) -> builtins.list[dict[str, Any]]:
        """Return poll-safe records with explicit worker freshness."""
        return [self.project(item, now=now) for item in self.list()]

    def get(self, operation_id: str) -> dict[str, Any] | None:
        """Return one raw durable operation record."""
        operation = self._read_operations().get(str(operation_id))
        return None if operation is None else dict(operation)

    def get_projected(
        self,
        operation_id: str,
        *,
        now: str,
    ) -> dict[str, Any] | None:
        """Return one operation with authoritative/LKG poll metadata."""
        operation = self.get(operation_id)
        return None if operation is None else self.project(operation, now=now)

    @staticmethod
    def project(operation: dict[str, Any], *, now: str) -> dict[str, Any]:
        """Project worker availability without treating absence as success."""
        projected = dict(operation)
        status = str(projected.get("status") or "")
        if status in TERMINAL_OPERATION_STATES:
            projected["worker_availability"] = "not_applicable"
            projected["freshness"] = "authoritative"
            return projected
        lease_expires_at = str(projected.get("lease_expires_at") or "")
        if lease_expires_at and _is_after(lease_expires_at, now):
            projected["worker_availability"] = "available"
            projected["freshness"] = "authoritative"
            return projected
        last_known_good = projected.get("last_known_good")
        projected["worker_availability"] = "unavailable"
        projected["freshness"] = "last_known_good"
        if isinstance(last_known_good, dict):
            for key in ("step", "message", "progress", "updated_at"):
                if key in last_known_good:
                    projected[key] = last_known_good[key]
        return projected

    def put(
        self,
        operation: dict[str, Any],
        *,
        worker_id: str | None = None,
        fencing_token: int | None = None,
    ) -> dict[str, Any]:
        """Persist an operation, rejecting stale or late worker completion."""
        operation_id = str(operation.get("operation_id") or "").strip()
        if not operation_id:
            raise ValueError("operation_id is required")

        def update(operations: dict[str, dict[str, Any]]) -> dict[str, Any]:
            current = operations.get(operation_id)
            if current is not None and worker_id is not None:
                self._require_worker(current, worker_id, fencing_token)
            if current is not None and str(current.get("status") or "") in {
                "cancel_requested",
                "cancelled",
                "completed",
                "failed",
            }:
                return dict(current)
            stored = {**(current or {}), **dict(operation)}
            stored.setdefault("created_at", stored.get("updated_at"))
            stored["last_known_good"] = _last_known_good(stored)
            operations[operation_id] = stored
            return dict(stored)

        return self._update_operations(update)

    def reserve_provider_operation(
        self,
        operation: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Atomically reserve idempotent, single-flight provider work."""
        operation_id = str(operation.get("operation_id") or "").strip()
        provider_id = str(operation.get("provider_id") or "").strip()
        idempotency_key = str(operation.get("idempotency_key") or "").strip()
        request_digest = str(operation.get("request_digest") or "").strip()
        if not operation_id:
            raise ValueError("operation_id is required")
        if not provider_id:
            raise ValueError("provider_id is required")
        if not idempotency_key or not request_digest:
            raise ValueError("idempotency_key and request_digest are required")

        def update(
            operations: dict[str, dict[str, Any]],
        ) -> tuple[dict[str, Any], bool]:
            for current in operations.values():
                if str(current.get("idempotency_key") or "") != idempotency_key:
                    continue
                if str(current.get("request_digest") or "") != request_digest:
                    raise RuntimeOperationConflict(
                        "runtime operation idempotency key conflicts with different requested work"
                    )
                return dict(current), False
            existing = operations.get(operation_id)
            if existing is not None:
                if str(existing.get("request_digest") or "") != request_digest:
                    raise RuntimeOperationConflict(
                        "runtime operation identifier conflicts with different work"
                    )
                return dict(existing), False
            for current in operations.values():
                if str(current.get("provider_id") or "") != provider_id:
                    continue
                if str(current.get("status") or "") in TERMINAL_OPERATION_STATES:
                    continue
                raise RuntimeOperationConflict(
                    "another runtime operation is already active for this provider"
                )
            stored = dict(operation)
            stored.setdefault("created_at", stored.get("updated_at"))
            stored.setdefault("fencing_token", 0)
            stored["last_known_good"] = _last_known_good(stored)
            operations[operation_id] = stored
            return dict(stored), True

        return self._update_operations(update)

    @contextmanager
    def provider_execution(self, provider_id: str):
        """Serialize provider side effects across service processes."""
        if self.path is None:
            with self._execution_lock:
                yield
            return
        safe_provider = "".join(
            character if character.isalnum() or character in ".-_" else "-"
            for character in str(provider_id)
        ).strip(".-_") or "runtime"
        lock_store = LockedJsonStore(
            self.path.with_name(f"{self.path.name}.{safe_provider}.execution")
        )
        with lock_store.locked():
            yield

    def acquire_lease(
        self,
        operation_id: str,
        *,
        worker_id: str,
        acquired_at: str,
        lease_expires_at: str,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Acquire an expired/unowned worker lease and advance its fence."""

        def update(
            operations: dict[str, dict[str, Any]],
        ) -> tuple[dict[str, Any] | None, bool]:
            current = operations.get(str(operation_id))
            if current is None:
                return None, False
            status = str(current.get("status") or "")
            if status in TERMINAL_OPERATION_STATES or status == "cancel_requested":
                return dict(current), False
            current_expiry = str(current.get("lease_expires_at") or "")
            current_worker = str(current.get("worker_id") or "")
            if (
                current_worker
                and current_worker != worker_id
                and current_expiry
                and _is_after(current_expiry, acquired_at)
            ):
                return dict(current), False
            stored = {
                **current,
                "status": "running",
                "worker_id": worker_id,
                "worker_availability": "available",
                "heartbeat_at": acquired_at,
                "lease_expires_at": lease_expires_at,
                "fencing_token": int(current.get("fencing_token") or 0) + 1,
                "updated_at": acquired_at,
            }
            stored["last_known_good"] = _last_known_good(stored)
            operations[str(operation_id)] = stored
            return dict(stored), True

        return self._update_operations(update)

    def append_progress(
        self,
        event: dict[str, Any],
        *,
        provider_id: str,
        updated_at: str,
        worker_id: str | None = None,
        fencing_token: int | None = None,
        lease_expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Append progress and optionally renew the owning worker lease."""
        operation_id = str(event.get("operation_id") or "").strip()
        if not operation_id:
            raise ValueError("operation_id is required")

        def update(operations: dict[str, dict[str, Any]]) -> dict[str, Any]:
            current = dict(operations.get(operation_id) or {})
            if worker_id is not None:
                self._require_worker(current, worker_id, fencing_token)
            if str(current.get("status") or "") in TERMINAL_OPERATION_STATES | {"cancel_requested"}:
                return dict(current)
            events = current.get("progress_events")
            progress_events = list(events) if isinstance(events, list) else []
            progress_events.append(dict(event))
            try:
                raw_percent = event.get("percent")
                if not isinstance(raw_percent, (int, float, str)):
                    raise TypeError("progress percent must be numeric")
                progress = int(float(raw_percent))
            except (TypeError, ValueError):
                progress = int(current.get("progress") or 0)
            stored = {
                **current,
                "operation_id": operation_id,
                "status": "running",
                "step": str(event.get("stage") or current.get("step") or "provider_setup"),
                "message": str(
                    event.get("message")
                    or current.get("message")
                    or "Runtime operation is running."
                ),
                "progress": max(0, min(100, progress)),
                "progress_events": progress_events,
                "reboot_required": bool(current.get("reboot_required", False)),
                "provider_id": str(current.get("provider_id") or provider_id),
                "updated_at": updated_at,
                "error": None,
            }
            if worker_id is not None:
                stored["heartbeat_at"] = updated_at
                if lease_expires_at:
                    stored["lease_expires_at"] = lease_expires_at
            stored["last_known_good"] = _last_known_good(stored)
            operations[operation_id] = stored
            return dict(stored)

        return self._update_operations(update)

    def heartbeat(
        self,
        operation_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> dict[str, Any]:
        """Renew a live worker lease when its fencing token remains current."""

        def update(operations: dict[str, dict[str, Any]]) -> dict[str, Any]:
            current = operations.get(str(operation_id))
            if current is None:
                raise RuntimeOperationLeaseLost("runtime operation is unavailable")
            self._require_worker(current, worker_id, fencing_token)
            if str(current.get("status") or "") in TERMINAL_OPERATION_STATES:
                raise RuntimeOperationLeaseLost("runtime operation is terminal")
            stored = {
                **current,
                "heartbeat_at": heartbeat_at,
                "lease_expires_at": lease_expires_at,
                "updated_at": heartbeat_at,
            }
            operations[str(operation_id)] = stored
            return dict(stored)

        return self._update_operations(update)

    def request_cancel(
        self,
        operation_id: str,
        *,
        updated_at: str,
        worker_active: bool,
    ) -> dict[str, Any] | None:
        """Request cancellation and wait for an active worker acknowledgement."""

        def update(
            operations: dict[str, dict[str, Any]],
        ) -> dict[str, Any] | None:
            current = operations.get(str(operation_id))
            if current is None:
                return None
            status = str(current.get("status") or "")
            if status in TERMINAL_OPERATION_STATES:
                stored = {
                    **current,
                    "cancelled": False,
                    "message": ("Runtime operation is already finished and cannot be cancelled."),
                    "updated_at": updated_at,
                }
            elif worker_active:
                stored = {
                    **current,
                    "status": "cancel_requested",
                    "cancelled": False,
                    "cancel_requested_at": updated_at,
                    "message": (
                        "Runtime operation cancellation is awaiting worker acknowledgement."
                    ),
                    "updated_at": updated_at,
                }
            else:
                stored = {
                    **current,
                    "status": "cancelled",
                    "cancelled": True,
                    "cancel_requested_at": updated_at,
                    "cancel_acknowledged_at": updated_at,
                    "message": (
                        "Runtime operation was cancelled after its worker became unavailable."
                    ),
                    "updated_at": updated_at,
                }
            stored["last_known_good"] = _last_known_good(stored)
            operations[str(operation_id)] = stored
            return dict(stored)

        return self._update_operations(update)

    def cancel(
        self,
        operation_id: str,
        *,
        updated_at: str,
    ) -> dict[str, Any] | None:
        """Compatibility cancellation for callers without worker tracking."""
        return self.request_cancel(
            operation_id,
            updated_at=updated_at,
            worker_active=False,
        )

    def acknowledge_cancel(
        self,
        operation_id: str,
        *,
        updated_at: str,
        worker_id: str | None = None,
        fencing_token: int | None = None,
    ) -> dict[str, Any] | None:
        """Record terminal cancellation after worker acknowledgement."""

        def update(
            operations: dict[str, dict[str, Any]],
        ) -> dict[str, Any] | None:
            current = operations.get(str(operation_id))
            if current is None:
                return None
            if worker_id is not None:
                self._require_worker(current, worker_id, fencing_token)
            if str(current.get("status") or "") in {"completed", "failed"}:
                return dict(current)
            stored = {
                **current,
                "status": "cancelled",
                "cancelled": True,
                "step": "cancelled",
                "message": "Runtime operation cancellation was acknowledged.",
                "cancel_acknowledged_at": updated_at,
                "lease_expires_at": updated_at,
                "updated_at": updated_at,
            }
            stored["last_known_good"] = _last_known_good(stored)
            operations[str(operation_id)] = stored
            return dict(stored)

        return self._update_operations(update)

    def recoverable(self, *, now: str) -> builtins.list[dict[str, Any]]:
        """Return nonterminal operations whose worker lease has expired."""
        candidates: builtins.list[dict[str, Any]] = []
        for operation in self.list():
            status = str(operation.get("status") or "")
            if status in TERMINAL_OPERATION_STATES:
                continue
            expiry = str(operation.get("lease_expires_at") or "")
            if not expiry or not _is_after(expiry, now):
                candidates.append(operation)
        return candidates

    def expire_deadlines(self, *, now: str) -> builtins.list[dict[str, Any]]:
        """Fail nonterminal operations whose durable deadline elapsed."""

        def update(
            operations: dict[str, dict[str, Any]],
        ) -> builtins.list[dict[str, Any]]:
            expired: builtins.list[dict[str, Any]] = []
            for operation_id, current in tuple(operations.items()):
                if str(current.get("status") or "") in TERMINAL_OPERATION_STATES:
                    continue
                deadline = str(current.get("deadline_at") or "")
                if not deadline or _is_after(deadline, now):
                    continue
                error = {
                    "code": "RUNTIME_OPERATION_DEADLINE_EXCEEDED",
                    "message": "Runtime operation exceeded its durable deadline.",
                }
                lease_expires_at = str(current.get("lease_expires_at") or "")
                if lease_expires_at and _is_after(lease_expires_at, now):
                    stored = {
                        **current,
                        "status": "cancel_requested",
                        "step": "deadline_exceeded",
                        "message": (
                            "Runtime operation exceeded its durable deadline and is "
                            "awaiting worker acknowledgement."
                        ),
                        "cancel_requested_at": now,
                        "updated_at": now,
                        "error": error,
                    }
                else:
                    stored = {
                        **current,
                        "status": "failed",
                        "step": "deadline_exceeded",
                        "message": error["message"],
                        "updated_at": now,
                        "error": error,
                    }
                stored["last_known_good"] = _last_known_good(stored)
                operations[operation_id] = stored
                expired.append(dict(stored))
            return expired

        return self._update_operations(update)

    def interrupt_nonterminal(
        self,
        *,
        updated_at: str,
        message: str = "Runtime operation was interrupted before completion.",
    ) -> builtins.list[dict[str, Any]]:
        """Retain the legacy explicit interrupt migration helper.

        New service startup must use lease-expiry recovery instead.
        """

        def update(
            operations: dict[str, dict[str, Any]],
        ) -> builtins.list[dict[str, Any]]:
            interrupted: builtins.list[dict[str, Any]] = []
            for operation_id, current in tuple(operations.items()):
                status = str(current.get("status") or "")
                if not status or status in TERMINAL_OPERATION_STATES:
                    continue
                stored = {
                    **current,
                    "status": "failed",
                    "step": str(current.get("step") or "interrupted"),
                    "message": message,
                    "updated_at": updated_at,
                    "error": {
                        "code": "RUNTIME_OPERATION_INTERRUPTED",
                        "message": message,
                    },
                }
                stored["last_known_good"] = _last_known_good(stored)
                operations[operation_id] = stored
                interrupted.append(dict(stored))
            return interrupted

        return self._update_operations(update)

    @staticmethod
    def _require_worker(
        current: dict[str, Any],
        worker_id: str,
        fencing_token: int | None,
    ) -> None:
        if str(current.get("worker_id") or "") != worker_id or int(
            current.get("fencing_token") or 0
        ) != int(fencing_token or 0):
            raise RuntimeOperationLeaseLost(
                "runtime operation worker lease is stale or unavailable"
            )

    def _read_operations(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            data = self._store.read() if self._store is not None else self._memory
            raw = data.get("operations") if isinstance(data, dict) else None
            if not isinstance(raw, dict):
                return {}
            return {
                str(operation_id): dict(operation)
                for operation_id, operation in raw.items()
                if isinstance(operation, dict)
            }

    def _update_operations(
        self,
        callback: Callable[[dict[str, dict[str, Any]]], _T],
    ) -> _T:
        def update(data: dict[str, Any]) -> tuple[dict[str, Any], _T]:
            raw = data.get("operations") if isinstance(data, dict) else None
            operations = {
                str(operation_id): dict(operation)
                for operation_id, operation in (raw.items() if isinstance(raw, dict) else ())
                if isinstance(operation, dict)
            }
            result = callback(operations)
            return {**data, "operations": operations}, result

        with self._lock:
            if self._store is not None:
                return self._store.update(update)
            next_data, result = update(dict(self._memory))
            self._memory = next_data
            return result


def _last_known_good(operation: dict[str, Any]) -> dict[str, Any]:
    return {
        key: operation.get(key)
        for key in ("status", "step", "message", "progress", "updated_at")
        if key in operation
    }


def _parse_timestamp(value: str) -> datetime:
    normalized = str(value or "").strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_after(left: str, right: str) -> bool:
    try:
        return _parse_timestamp(left) > _parse_timestamp(right)
    except (TypeError, ValueError):
        return False
