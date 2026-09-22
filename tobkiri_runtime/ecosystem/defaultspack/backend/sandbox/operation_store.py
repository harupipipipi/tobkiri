from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator, List


TERMINAL_OPERATION_STATES = {"completed", "failed", "cancelled"}


class RuntimeOperationStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._operations: dict[str, dict[str, Any]] = {}
        self._load()

    def list(self) -> list[dict[str, Any]]:
        with self._storage_lock():
            return sorted(
                (dict(item) for item in self._operations.values()),
                key=lambda item: str(item.get("updated_at") or ""),
                reverse=True,
            )

    def get(self, operation_id: str) -> dict[str, Any] | None:
        with self._storage_lock():
            operation = self._operations.get(str(operation_id))
            return None if operation is None else dict(operation)

    def put(self, operation: dict[str, Any]) -> dict[str, Any]:
        operation_id = str(operation.get("operation_id") or "").strip()
        if not operation_id:
            raise ValueError("operation_id is required")
        with self._storage_lock():
            current = self._operations.get(operation_id)
            if isinstance(current, dict) and str(current.get("status") or "") == "cancelled":
                preserved = dict(current)
                incoming_events = operation.get("progress_events")
                if isinstance(incoming_events, list):
                    existing_events = preserved.get("progress_events")
                    progress_events: list[Any] = []
                    seen: set[tuple[str, str, str, str]] = set()
                    for event in [
                        *(existing_events if isinstance(existing_events, list) else []),
                        *incoming_events,
                    ]:
                        if not isinstance(event, dict):
                            continue
                        key = (
                            str(event.get("operation_id") or ""),
                            str(event.get("stage") or ""),
                            str(event.get("message") or ""),
                            str(event.get("percent") or ""),
                        )
                        if key in seen:
                            continue
                        seen.add(key)
                        progress_events.append(event)
                    preserved["progress_events"] = progress_events
                preserved["updated_at"] = operation.get("updated_at") or preserved.get("updated_at")
                self._operations[operation_id] = preserved
                self._save()
                return dict(preserved)
            stored = dict(operation)
            self._operations[operation_id] = stored
            self._save()
            return dict(stored)

    def reserve_provider_operation(self, operation: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        operation_id = str(operation.get("operation_id") or "").strip()
        provider_id = str(operation.get("provider_id") or "").strip()
        if not operation_id:
            raise ValueError("operation_id is required")
        if not provider_id:
            raise ValueError("provider_id is required")
        with self._storage_lock():
            for current in self._operations.values():
                if str(current.get("provider_id") or "") != provider_id:
                    continue
                if str(current.get("status") or "") in TERMINAL_OPERATION_STATES:
                    continue
                return dict(current), False
            stored = dict(operation)
            self._operations[operation_id] = stored
            self._save()
            return dict(stored), True

    def reserve_desktop_operation(
        self,
        operation: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        """Atomically reserve a lifecycle mutation or return its replay/conflict state."""
        operation_id = str(operation.get("operation_id") or "").strip()
        seat_id = str(operation.get("seat_id") or "").strip()
        action = str(operation.get("action") or "").strip()
        if not operation_id:
            raise ValueError("operation_id is required")
        if not seat_id:
            raise ValueError("seat_id is required")
        if not action:
            raise ValueError("action is required")
        with self._storage_lock():
            existing = self._operations.get(operation_id)
            if existing is not None:
                same_request = (
                    str(existing.get("operation_kind") or "") == "desktop_lifecycle"
                    and str(existing.get("seat_id") or "") == seat_id
                    and str(existing.get("action") or "") == action
                )
                return dict(existing), "replay" if same_request else "id_conflict"
            for current in self._operations.values():
                if str(current.get("operation_kind") or "") != "desktop_lifecycle":
                    continue
                if str(current.get("seat_id") or "") != seat_id:
                    continue
                if str(current.get("status") or "") in TERMINAL_OPERATION_STATES:
                    continue
                return dict(current), "seat_busy"
            stored = dict(operation)
            self._operations[operation_id] = stored
            self._save()
            return dict(stored), "reserved"

    def append_progress(
        self,
        event: dict[str, Any],
        *,
        provider_id: str,
        updated_at: str,
    ) -> dict[str, Any]:
        operation_id = str(event.get("operation_id") or "").strip()
        if not operation_id:
            raise ValueError("operation_id is required")
        with self._storage_lock():
            current = dict(self._operations.get(operation_id) or {})
            if str(current.get("status") or "") in TERMINAL_OPERATION_STATES:
                return dict(current)
            events = current.get("progress_events")
            progress_events = list(events) if isinstance(events, list) else []
            progress_events.append(dict(event))
            raw_percent = event.get("percent")
            try:
                numeric_percent = (
                    raw_percent
                    if isinstance(raw_percent, (int, float, str))
                    else 0
                )
                progress = int(float(numeric_percent))
            except (TypeError, ValueError):
                progress = int(current.get("progress") or 0)
            stored = {
                **current,
                "operation_id": operation_id,
                "status": "running",
                "step": str(event.get("stage") or current.get("step") or "provider_setup"),
                "message": str(event.get("message") or current.get("message") or "Runtime operation is running."),
                "progress": max(0, min(100, progress)),
                "progress_events": progress_events,
                "reboot_required": bool(current.get("reboot_required", False)),
                "provider_id": str(current.get("provider_id") or provider_id),
                "updated_at": updated_at,
                "error": None,
            }
            self._operations[operation_id] = stored
            self._save()
            return dict(stored)

    def cancel(self, operation_id: str, *, updated_at: str) -> dict[str, Any] | None:
        with self._storage_lock():
            current = self._operations.get(str(operation_id))
            if current is None:
                return None
            status = str(current.get("status") or "")
            if status in TERMINAL_OPERATION_STATES:
                current = {
                    **current,
                    "cancelled": False,
                    "message": "Runtime operation is already finished and cannot be cancelled.",
                    "updated_at": updated_at,
                }
            else:
                current = {
                    **current,
                    "status": "cancelled",
                    "cancelled": True,
                    "message": "Runtime operation cancellation was recorded.",
                    "progress": current.get("progress", 0),
                    "updated_at": updated_at,
                }
            self._operations[str(operation_id)] = current
            self._save()
            return dict(current)

    def interrupt_nonterminal(
        self,
        *,
        updated_at: str,
        message: str = "Runtime operation was interrupted before completion.",
    ) -> List[dict[str, Any]]:
        interrupted: List[dict[str, Any]] = []
        with self._storage_lock():
            for operation_id, current in tuple(self._operations.items()):
                status = str(current.get("status") or "")
                if not status or status in TERMINAL_OPERATION_STATES:
                    continue
                updated = {
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
                self._operations[operation_id] = updated
                interrupted.append(dict(updated))
            if interrupted:
                self._save()
        return interrupted

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        raw_operations = payload.get("operations") if isinstance(payload, dict) else None
        if not isinstance(raw_operations, dict):
            return
        self._operations = {
            str(operation_id): dict(operation)
            for operation_id, operation in raw_operations.items()
            if isinstance(operation, dict)
        }

    @contextmanager
    def _storage_lock(self) -> Iterator[None]:
        """Serialize reads and writes across runtime service processes."""
        with self._lock:
            if self.path is None:
                yield
                return
            lock_path = self.path.with_name(f"{self.path.name}.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as handle:
                _lock_file_handle(handle)
                try:
                    self._load()
                    yield
                finally:
                    _unlock_file_handle(handle)

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"operations": self._operations}
        tmp = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self.path)


def _lock_file_handle(handle: BinaryIO) -> None:
    if os.name == "nt":
        try:
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            locking = getattr(msvcrt, "locking")
            lock_mode = getattr(msvcrt, "LK_LOCK")
            locking(handle.fileno(), lock_mode, 1)
        except (ImportError, OSError):
            return
        return
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except (ImportError, OSError):
        return


def _unlock_file_handle(handle: BinaryIO) -> None:
    if os.name == "nt":
        try:
            import msvcrt

            handle.seek(0)
            locking = getattr(msvcrt, "locking")
            unlock_mode = getattr(msvcrt, "LK_UNLCK")
            locking(handle.fileno(), unlock_mode, 1)
        except (ImportError, OSError):
            return
        return
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        return
