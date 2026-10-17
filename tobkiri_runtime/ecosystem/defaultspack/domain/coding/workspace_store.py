from __future__ import annotations

import copy
import errno
import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
STORE_ENV_VAR = "RUMI_DEFAULTSPACK_CODING_WORKSPACE_STORE_PATH"
_LOCK_REGISTRY_GUARD = threading.Lock()
_LOCK_REGISTRY: dict[str, threading.RLock] = {}


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def default_storage_path() -> Path:
    override = os.environ.get(STORE_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2] / "user_data" / "shared" / "coding_workspaces.json"


def _lock_for_path(path: Path) -> threading.RLock:
    key = str(path.expanduser().resolve())
    with _LOCK_REGISTRY_GUARD:
        lock = _LOCK_REGISTRY.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCK_REGISTRY[key] = lock
        return lock


def normalize_workspace_root(root_path: str | os.PathLike[str]) -> str:
    if root_path is None or str(root_path).strip() == "":
        raise ValueError("workspace root path is required")
    candidate = Path(str(root_path)).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("workspace root path does not exist: " + str(root_path)) from exc
    if not resolved.is_dir():
        raise ValueError("workspace root path is not a directory: " + str(root_path))
    return str(resolved)


def default_label_for_root(root_path: str) -> str:
    path = Path(root_path)
    return path.name or str(path)


def _safe_workspace_id(workspace_id: str | None = None) -> str:
    value = str(workspace_id or "").strip()
    if not value:
        return str(uuid.uuid4())
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if any(ch not in allowed for ch in value):
        raise ValueError("workspace_id may contain only letters, numbers, '.', '_' and '-'")
    return value


def _run_git(root_path: str, args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git"] + args,
            cwd=root_path,
            text=True,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    output = completed.stdout.strip()
    return output or None


def detect_workspace_metadata(root_path: str) -> dict[str, Any]:
    git_root = _run_git(root_path, ["rev-parse", "--show-toplevel"])
    if git_root:
        git_root = os.path.realpath(git_root)
    default_branch = _run_git(root_path, ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])
    if default_branch and default_branch.startswith("origin/"):
        default_branch = default_branch.split("/", 1)[1]
    if not default_branch:
        current_branch = _run_git(root_path, ["rev-parse", "--abbrev-ref", "HEAD"])
        default_branch = None if current_branch == "HEAD" else current_branch
    return {
        "git_root": git_root,
        "default_branch": default_branch,
    }


class WorkspaceStore:
    def __init__(self, storage_path: str | os.PathLike[str] | None = None) -> None:
        self._storage_path = Path(storage_path).expanduser() if storage_path else default_storage_path()
        self._lock = _lock_for_path(self._storage_path)

    @property
    def storage_path(self) -> Path:
        return self._storage_path

    def list(self) -> list[dict[str, Any]]:
        data = self._load()
        records = [copy.deepcopy(record) for record in data["workspaces"].values()]
        records.sort(key=lambda item: (item.get("last_used_at") or "", item.get("label") or ""), reverse=True)
        return records

    def get(self, workspace_id: str) -> dict[str, Any] | None:
        data = self._load()
        record = data["workspaces"].get(str(workspace_id))
        return copy.deepcopy(record) if record else None

    def find_by_root(self, root_path: str | os.PathLike[str]) -> dict[str, Any] | None:
        normalized_root = normalize_workspace_root(root_path)
        for record in self.list():
            try:
                candidate_root = record.get("root_path")
                if not isinstance(candidate_root, (str, os.PathLike)):
                    continue
                candidate = normalize_workspace_root(candidate_root)
            except ValueError:
                continue
            if candidate == normalized_root:
                return record
        return None

    def create(
        self,
        root_path: str | os.PathLike[str],
        label: str | None = None,
        workspace_id: str | None = None,
        trusted: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_root = normalize_workspace_root(root_path)
        workspace_id = _safe_workspace_id(workspace_id)
        now = utc_now()
        detected_metadata = detect_workspace_metadata(normalized_root)
        if isinstance(metadata, dict):
            detected_metadata.update(metadata)
        record = {
            "workspace_id": workspace_id,
            "label": str(label or "").strip() or default_label_for_root(normalized_root),
            "root_path": normalized_root,
            "trusted": bool(trusted),
            "trust_granted_at": now if trusted else None,
            "last_used_at": now,
            "metadata": detected_metadata,
        }
        with self._lock:
            data = self._load_unlocked()
            if workspace_id in data["workspaces"]:
                raise ValueError("workspace_id already exists: " + workspace_id)
            data["workspaces"][workspace_id] = record
            data["updated_at"] = now
            self._save_unlocked(data)
        return copy.deepcopy(record)

    def update(self, workspace_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        workspace_id = str(workspace_id or "").strip()
        if not workspace_id:
            raise ValueError("workspace_id is required")
        updates = updates or {}
        with self._lock:
            data = self._load_unlocked()
            record = data["workspaces"].get(workspace_id)
            if record is None:
                raise KeyError(workspace_id)
            if "root_path" in updates and updates.get("root_path"):
                record["root_path"] = normalize_workspace_root(updates["root_path"])
                record["metadata"] = detect_workspace_metadata(record["root_path"])
                record["trusted"] = False
                record["trust_granted_at"] = None
            if "label" in updates:
                label = str(updates.get("label") or "").strip()
                record["label"] = label or default_label_for_root(record["root_path"])
            if isinstance(updates.get("metadata"), dict):
                metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
                metadata.update(updates["metadata"])
                record["metadata"] = metadata
            record["last_used_at"] = utc_now()
            data["updated_at"] = record["last_used_at"]
            self._save_unlocked(data)
        return copy.deepcopy(record)

    def touch(self, workspace_id: str) -> dict[str, Any] | None:
        workspace_id = str(workspace_id or "").strip()
        if not workspace_id:
            return None
        with self._lock:
            data = self._load_unlocked()
            record = data["workspaces"].get(workspace_id)
            if record is None:
                return None
            record["last_used_at"] = utc_now()
            data["updated_at"] = record["last_used_at"]
            self._save_unlocked(data)
            return copy.deepcopy(record)

    def trust(self, workspace_id: str) -> dict[str, Any]:
        workspace_id = str(workspace_id or "").strip()
        if not workspace_id:
            raise ValueError("workspace_id is required")
        with self._lock:
            data = self._load_unlocked()
            record = data["workspaces"].get(workspace_id)
            if record is None:
                raise KeyError(workspace_id)
            normalize_workspace_root(record.get("root_path"))
            now = utc_now()
            record["trusted"] = True
            record["trust_granted_at"] = now
            record["last_used_at"] = now
            data["updated_at"] = now
            self._save_unlocked(data)
        return copy.deepcopy(record)

    def select(self, workspace_id: str) -> dict[str, Any]:
        workspace_id = str(workspace_id or "").strip()
        if not workspace_id:
            raise ValueError("workspace_id is required")
        with self._lock:
            data = self._load_unlocked()
            record = data["workspaces"].get(workspace_id)
            if record is None:
                raise KeyError(workspace_id)
            normalize_workspace_root(record.get("root_path"))
            now = utc_now()
            record["last_used_at"] = now
            data["selected_workspace_id"] = workspace_id
            data["updated_at"] = now
            self._save_unlocked(data)
        return copy.deepcopy(record)

    def selected_workspace_id(self) -> str | None:
        data = self._load()
        selected = data.get("selected_workspace_id")
        return str(selected) if selected else None

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._load())

    def _load(self) -> dict[str, Any]:
        with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> dict[str, Any]:
        try:
            payload = json.loads(self._storage_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty_payload()
        except Exception:
            return self._empty_payload()
        return self._normalize_payload(payload)

    def _empty_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": None,
            "selected_workspace_id": None,
            "workspaces": {},
        }

    def _normalize_payload(self, payload: Any) -> dict[str, Any]:
        data = self._empty_payload()
        if not isinstance(payload, dict):
            return data
        raw_workspaces = payload.get("workspaces", {})
        if isinstance(raw_workspaces, list):
            raw_workspaces = {
                str(item.get("workspace_id")): item
                for item in raw_workspaces
                if isinstance(item, dict) and item.get("workspace_id")
            }
        if isinstance(raw_workspaces, dict):
            for key, raw in raw_workspaces.items():
                if not isinstance(raw, dict):
                    continue
                workspace_id = str(raw.get("workspace_id") or key)
                if not workspace_id:
                    continue
                root_path = str(raw.get("root_path") or "")
                label = raw.get("label") or (default_label_for_root(root_path) if root_path else workspace_id)
                record: dict[str, Any] = {
                    "workspace_id": workspace_id,
                    "label": str(label),
                    "root_path": root_path,
                    "trusted": bool(raw.get("trusted", False)),
                    "trust_granted_at": raw.get("trust_granted_at"),
                    "last_used_at": raw.get("last_used_at"),
                    "metadata": raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
                }
                data["workspaces"][workspace_id] = record
        selected = payload.get("selected_workspace_id")
        if selected and str(selected) in data["workspaces"]:
            data["selected_workspace_id"] = str(selected)
        data["schema_version"] = int(payload.get("schema_version") or SCHEMA_VERSION)
        data["updated_at"] = payload.get("updated_at")
        return data

    def _save_unlocked(self, data: dict[str, Any]) -> None:
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._storage_path.parent),
            prefix="." + self._storage_path.name + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            self._replace_atomic_file(Path(tmp_name), self._storage_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _is_transient_replace_error(exc: OSError) -> bool:
        winerror = getattr(exc, "winerror", None)
        errno_value = getattr(exc, "errno", None)
        if isinstance(exc, PermissionError):
            return True
        if winerror in {5, 32}:
            return True
        if errno_value in {errno.EACCES, errno.EBUSY, errno.EPERM}:
            return True
        message = str(exc).lower()
        return "access is denied" in message or "permission denied" in message

    def _replace_atomic_file(self, tmp_path: Path, path: Path) -> None:
        last_error: OSError | None = None
        for attempt in range(8):
            try:
                tmp_path.replace(path)
                return
            except OSError as exc:
                last_error = exc
                if not self._is_transient_replace_error(exc) or attempt >= 7:
                    break
                time.sleep(min(0.05 * (2 ** attempt), 0.5))
        if last_error is not None:
            raise last_error
