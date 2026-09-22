"""Profile-bound authoritative workspace mount metadata."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import validate_profile_id
from core_runtime.runtime_locks import NamedLock

AUTHORITY = "rumi.service.host.authorize.v1"
VERSION = "rumi.workspace-mounts.v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class WorkspaceConflict(RuntimeError):
    """Raised for stale workspace mount mutations."""


class WorkspaceMountStore:
    """Own canonical workspace mount metadata for one profile."""

    def __init__(self, profile_id: str, *, user_data_root: Path | None = None) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.root = (
            Path(user_data_root or USER_DATA_DIR)
            / "packs"
            / "rumi_workspace_mount_pack"
            / "profiles"
            / self.profile_id
        )
        self.path = self.root / "mounts.json"
        self.lock_root = self.root / "locks"

    def snapshot(self) -> dict[str, Any]:
        """Return all canonical mounts without filesystem probing."""
        state = self._read()
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": state["revision"],
            "selected_workspace_id": state["selected_workspace_id"],
            "mounts": [state["mounts"][key] for key in sorted(state["mounts"])],
        }

    def get(self, workspace_id: str) -> dict[str, Any] | None:
        """Return one exact workspace mount."""
        value = self._read()["mounts"].get(_identifier(workspace_id))
        return _copy(value) if isinstance(value, Mapping) else None

    def mount(
        self,
        workspace_id: str,
        root_path: str,
        *,
        expected_revision: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register an existing canonical directory at an exact revision."""
        workspace_id = _identifier(workspace_id)
        canonical = Path(root_path).expanduser().resolve(strict=True)
        if not canonical.is_dir():
            raise ValueError("workspace root is not a directory")
        with NamedLock(self.lock_root, "mounts"):
            state = self._read()
            _assert_revision(state, expected_revision)
            now = int(time.time() * 1000)
            current = state["mounts"].get(workspace_id)
            record = {
                "id": workspace_id,
                "root_path": str(canonical),
                "metadata": _copy(metadata or {}),
                "created_at": current.get("created_at") if current else now,
                "updated_at": now,
                "mount_revision": int(current.get("mount_revision") or 0) + 1 if current else 1,
            }
            state["mounts"][workspace_id] = record
            state["revision"] += 1
            self._write(state)
        return {"mount": _copy(record), "revision": state["revision"]}

    def unmount(self, workspace_id: str, *, expected_revision: int) -> dict[str, Any]:
        """Remove mount metadata without deleting workspace files."""
        workspace_id = _identifier(workspace_id)
        with NamedLock(self.lock_root, "mounts"):
            state = self._read()
            _assert_revision(state, expected_revision)
            if workspace_id not in state["mounts"]:
                raise KeyError("workspace mount is unknown")
            del state["mounts"][workspace_id]
            if state["selected_workspace_id"] == workspace_id:
                state["selected_workspace_id"] = None
            state["revision"] += 1
            self._write(state)
        return {"unmounted": workspace_id, "revision": state["revision"]}

    def update(
        self,
        workspace_id: str,
        *,
        expected_revision: int,
        root_path: str | None,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Update exact mount metadata without creating an implicit record."""

        workspace_id = _identifier(workspace_id)
        with NamedLock(self.lock_root, "mounts"):
            state = self._read()
            _assert_revision(state, expected_revision)
            current = state["mounts"].get(workspace_id)
            if current is None:
                raise KeyError("workspace mount is unknown")
            canonical = Path(current["root_path"])
            if root_path:
                canonical = Path(root_path).expanduser().resolve(strict=True)
                if not canonical.is_dir():
                    raise ValueError("workspace root is not a directory")
            current["root_path"] = str(canonical)
            current["metadata"] = {**current["metadata"], **_copy(metadata)}
            current["updated_at"] = int(time.time() * 1000)
            current["mount_revision"] = int(current["mount_revision"]) + 1
            state["revision"] += 1
            self._write(state)
        return {"mount": _copy(current), "revision": state["revision"]}

    def select(self, workspace_id: str, *, expected_revision: int) -> dict[str, Any]:
        """Select one existing mount for the profile."""

        workspace_id = _identifier(workspace_id)
        with NamedLock(self.lock_root, "mounts"):
            state = self._read()
            _assert_revision(state, expected_revision)
            if workspace_id not in state["mounts"]:
                raise KeyError("workspace mount is unknown")
            state["selected_workspace_id"] = workspace_id
            state["revision"] += 1
            self._write(state)
        return {
            "mount": _copy(state["mounts"][workspace_id]),
            "selected_workspace_id": workspace_id,
            "revision": state["revision"],
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "version": VERSION,
                "profile_id": self.profile_id,
                "revision": 0,
                "selected_workspace_id": None,
                "mounts": {},
            }
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or value.get("version") != VERSION:
            raise ValueError("workspace mount state is invalid")
        if value.get("profile_id") != self.profile_id:
            raise ValueError("workspace mount profile does not match")
        mounts = value.get("mounts")
        if not isinstance(mounts, Mapping):
            raise ValueError("workspace mount records are invalid")
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": max(0, int(value.get("revision") or 0)),
            "selected_workspace_id": value.get("selected_workspace_id"),
            "mounts": {str(key): _copy(item) for key, item in mounts.items()},
        }

    def _write(self, state: Mapping[str, Any]) -> None:
        _atomic_json(self.path, state)


def create_workspace_resource(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create workspace metadata read operations."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        store = WorkspaceMountStore(_profile(payload))
        if name == "list":
            return store.snapshot()
        if name == "get":
            return store.get(str(payload.get("workspace_id") or ""))
        raise ValueError(f"unknown workspace resource operation: {name}")

    return operation


def create_workspace_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated workspace mount mutations."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name not in {"mount", "unmount", "update", "select", "trust"}:
            raise ValueError(f"unknown workspace action: {name}")
        arguments = _action_arguments(name, payload)
        _redeem(client, payload, f"workspace.{name}", arguments)
        store = WorkspaceMountStore(_profile(payload))
        expected = int(arguments["expected_revision"])
        if name == "mount":
            return store.mount(
                arguments["workspace_id"],
                arguments["root_path"],
                expected_revision=expected,
                metadata=arguments["metadata"],
            )
        if name == "unmount":
            return store.unmount(arguments["workspace_id"], expected_revision=expected)
        if name == "select":
            return store.select(arguments["workspace_id"], expected_revision=expected)
        metadata = {"trusted": True} if name == "trust" else arguments["metadata"]
        return store.update(
            arguments["workspace_id"],
            expected_revision=expected,
            root_path=arguments.get("root_path") or None,
            metadata=metadata,
        )

    return operation


def capture_selected_workspace_binding(
    profile_id: str,
    *,
    user_data_root: Path | None = None,
) -> dict[str, object]:
    """Capture the selected root with immutable mount and filesystem identity."""

    store = WorkspaceMountStore(profile_id, user_data_root=user_data_root)
    snapshot = store.snapshot()
    workspace_id = str(snapshot.get("selected_workspace_id") or "").strip()
    if not workspace_id:
        raise ValueError("a Host-selected workspace is required")
    mount = store.get(workspace_id)
    if not isinstance(mount, Mapping):
        raise ValueError("the Host-selected workspace is unavailable")
    unresolved = Path(str(mount.get("root_path") or ""))
    if unresolved.is_symlink():
        raise PermissionError("the selected workspace root must not be a symlink")
    root = unresolved.resolve(strict=True)
    if not root.is_dir():
        raise PermissionError("the selected workspace root is unavailable")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root, flags)
    try:
        root_stat = os.fstat(root_fd)
        current = root.stat()
        if (root_stat.st_dev, root_stat.st_ino) != (current.st_dev, current.st_ino):
            raise PermissionError("the selected workspace root changed during capture")
    finally:
        os.close(root_fd)
    binding: dict[str, object] = {
        "workspace_id": workspace_id,
        "access": "read_only",
        "mount_revision": str(
            mount.get("revision") or mount.get("updated_at_ms") or mount.get("updated_at") or ""
        ),
        "canonical_root": str(root),
        "root_st_dev": int(root_stat.st_dev),
        "root_st_ino": int(root_stat.st_ino),
    }
    binding["root_identity"] = hashlib.sha256(
        json.dumps(
            binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return binding


def _redeem(
    client: Any,
    payload: Mapping[str, Any],
    operation: str,
    arguments: Mapping[str, Any],
) -> None:
    result = client.invoke(
        AUTHORITY,
        "redeem",
        {
            "receipt": str(payload.get("authority_receipt") or ""),
            "service_pack_id": "rumi_workspace_mount_pack",
            "operation": operation,
            "authority": "workspace.mount.manage",
            "caller_id": str(payload.get("caller_id") or ""),
            "caller_pack_id": str(payload.get("caller_pack_id") or ""),
            "caller_function_id": str(payload.get("caller_function_id") or ""),
            "profile_id": _profile(payload),
            "workspace_id": str(payload.get("workspace_id") or ""),
            "session_id": str(payload.get("session_id") or ""),
            "arguments": dict(arguments),
        },
    )
    if not result.get("authorized"):
        raise PermissionError(str(result.get("reason") or "workspace authority denied"))


def _identifier(value: Any) -> str:
    identifier = str(value or "").strip()
    if not _ID.fullmatch(identifier):
        raise ValueError("workspace identifier is invalid")
    return identifier


def _action_arguments(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "workspace_id": str(payload.get("workspace_id") or ""),
        "expected_revision": max(0, int(payload.get("expected_revision") or 0)),
    }
    if name in {"mount", "update"}:
        arguments["root_path"] = str(payload.get("root_path") or "")
        arguments["metadata"] = dict(_mapping(payload.get("metadata")))
    return arguments


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("workspace metadata must be an object")
    return value


def _assert_revision(state: Mapping[str, Any], expected: int) -> None:
    if int(state.get("revision") or 0) != expected:
        raise WorkspaceConflict("workspace mount revision is stale")


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".mount-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
