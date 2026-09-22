"""Read-only workspace-jailed file inspection service."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping

WORKSPACE = "rumi.resource.workspace.v1"
_MAX_READ_BYTES = 4 * 1024 * 1024
_MAX_RESULTS = 10_000
_PROTECTED_PARTS = frozenset({".git", ".rumi_snapshots"})
_SECRET_PARTS = frozenset({
    ".aws",
    ".azure",
    ".docker",
    ".gnupg",
    ".kube",
    ".ssh",
    "secrets",
})
_SECRET_NAMES = frozenset({
    ".dockercfg",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "kubeconfig",
    "token",
    "tokens.json",
})
_SECRET_SUFFIXES = (".key", ".pem", ".p12", ".pfx", ".crt")
_SAFE_ENV_SUFFIXES = (".example", ".sample", ".template")


class FileInspectService:
    """Inspect files under an exact selected workspace mount."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch one read-only file operation."""
        _check_lifecycle(payload)
        root, root_fd, binding = self._workspace(payload)
        try:
            if name == "read":
                return self._read(root, payload, root_fd=root_fd)
            if name == "stat":
                return self._stat(root, payload)
            if name == "list":
                result = self._list(root, payload)
                self._validate_root_binding(root, root_fd, binding)
                return result
            if name == "search":
                result = self._search(root, payload)
                self._validate_root_binding(root, root_fd, binding)
                return result
            raise ValueError(f"unknown file inspect operation: {name}")
        finally:
            os.close(root_fd)

    def _workspace(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[Path, int, dict[str, Any]]:
        workspace_id = str(payload.get("workspace_id") or "").strip()
        if not workspace_id:
            raise ValueError("workspace_id is required")
        if payload.get("require_selected"):
            if self._selected_workspace_id(payload) != workspace_id:
                raise PermissionError(
                    "workspace is not the selected Host binding"
                )
        mount = self.client.invoke(
            WORKSPACE,
            "get",
            {"profile_id": _profile(payload), "workspace_id": workspace_id},
        )
        if not isinstance(mount, Mapping):
            raise KeyError("workspace mount is unknown")
        root = Path(str(mount.get("root_path") or "")).resolve(strict=True)
        if not root.is_dir():
            raise PermissionError("workspace root is unavailable")
        root_fd = os.open(
            root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        binding = dict(payload.get("_workspace_binding") or {})
        try:
            self._validate_mount_binding(
                workspace_id,
                mount,
                root,
                root_fd,
                binding,
            )
        except Exception:
            os.close(root_fd)
            raise
        if (
            payload.get("require_selected")
            and self._selected_workspace_id(payload) != workspace_id
        ):
            os.close(root_fd)
            raise PermissionError(
                "workspace selection changed during Host binding"
            )
        return root, root_fd, binding

    @staticmethod
    def _validate_mount_binding(
        workspace_id: str,
        mount: Mapping[str, Any],
        root: Path,
        root_fd: int,
        binding: Mapping[str, Any],
    ) -> None:
        if str(binding.get("workspace_id") or "") != workspace_id:
            raise PermissionError("Host workspace binding is required")
        if str(binding.get("access") or "") != "read_only":
            raise PermissionError("Host workspace binding must be read_only")
        root_stat = os.fstat(root_fd)
        actual = {
            "workspace_id": workspace_id,
            "access": "read_only",
            "mount_revision": str(
                mount.get("revision")
                or mount.get("updated_at_ms")
                or mount.get("updated_at")
                or ""
            ),
            "canonical_root": str(root),
            "root_st_dev": int(root_stat.st_dev),
            "root_st_ino": int(root_stat.st_ino),
        }
        actual_identity = hashlib.sha256(
            json.dumps(
                actual,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        for key, value in actual.items():
            if binding.get(key) != value:
                raise PermissionError(
                    f"workspace mount binding changed: {key}"
                )
        if str(binding.get("root_identity") or "") != actual_identity:
            raise PermissionError("workspace root identity changed")

    @staticmethod
    def _validate_root_binding(
        root: Path,
        root_fd: int,
        binding: Mapping[str, Any],
    ) -> None:
        opened = os.fstat(root_fd)
        current = root.stat()
        if (
            int(opened.st_dev) != int(binding.get("root_st_dev") or -1)
            or int(opened.st_ino) != int(binding.get("root_st_ino") or -1)
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
        ):
            raise PermissionError("workspace root changed during inspection")

    def _selected_workspace_id(
        self,
        payload: Mapping[str, Any],
    ) -> str:
        snapshot = self.client.invoke(
            WORKSPACE,
            "list",
            {"profile_id": _profile(payload)},
        )
        return (
            str(snapshot.get("selected_workspace_id") or "").strip()
            if isinstance(snapshot, Mapping)
            else ""
        )

    def _read(
        self,
        root: Path,
        payload: Mapping[str, Any],
        *,
        root_fd: int,
    ) -> dict[str, Any]:
        path = _jailed(root, payload.get("path"), must_exist=True)
        if not path.is_file():
            raise FileNotFoundError("file is unavailable")
        max_bytes = max(1, min(_MAX_READ_BYTES, int(payload.get("max_bytes") or _MAX_READ_BYTES)))
        content, size = _read_text_no_follow(
            root,
            path.relative_to(root),
            encoding=str(payload.get("encoding") or "utf-8"),
            max_bytes=max_bytes,
            root_fd=root_fd,
        )
        start = max(1, int(payload.get("start_line") or 1))
        end_value = payload.get("end_line")
        lines = content.splitlines(keepends=True)
        end = len(lines) if end_value is None else max(start, int(end_value))
        selected = "".join(lines[start - 1 : end])
        return {
            "workspace_id": str(payload["workspace_id"]),
            "path": path.relative_to(root).as_posix(),
            "content": selected,
            "size": size,
            "encoding": str(payload.get("encoding") or "utf-8"),
            "start_line": start,
            "end_line": min(end, len(lines)),
            "total_lines": len(lines),
            "read_only": True,
        }

    def _stat(self, root: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
        path = _jailed(root, payload.get("path"), must_exist=True)
        stat = path.stat()
        return {
            "workspace_id": str(payload["workspace_id"]),
            "path": path.relative_to(root).as_posix() if path != root else ".",
            "is_file": path.is_file(),
            "is_dir": path.is_dir(),
            "size": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
            "read_only": True,
        }

    def _list(self, root: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
        directory = _jailed(root, payload.get("directory") or ".", must_exist=True)
        if not directory.is_dir():
            raise NotADirectoryError("workspace path is not a directory")
        recursive = bool(payload.get("recursive", False))
        if payload.get("tracked_only"):
            iterator = _git_tracked_files(
                root,
                directory,
                recursive=recursive,
                deadline_epoch_ms=int(
                    payload.get("_deadline_epoch_ms") or 0
                ),
            )
        else:
            iterator = (
                _deterministic_files(directory, recursive=recursive)
            )
        items = []
        for candidate in iterator:
            try:
                resolved = candidate.resolve(strict=True)
            except (FileNotFoundError, OSError, RuntimeError):
                continue
            if not _within(root, resolved):
                continue
            relative = resolved.relative_to(root)
            try:
                _deny_restricted_path(relative)
            except PermissionError:
                continue
            try:
                stat = resolved.stat()
            except OSError:
                continue
            items.append(
                {
                    "path": relative.as_posix(),
                    "name": resolved.name,
                    "is_file": resolved.is_file(),
                    "is_dir": resolved.is_dir(),
                    "size": stat.st_size,
                }
            )
            if len(items) >= _MAX_RESULTS:
                break
        items.sort(key=lambda item: item["path"])
        return {"workspace_id": str(payload["workspace_id"]), "items": items}

    def _search(self, root: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
        pattern = str(payload.get("pattern") or "").strip()
        if not pattern:
            raise ValueError("file search pattern is required")
        directory = _jailed(root, payload.get("directory") or ".", must_exist=True)
        matches = []
        for candidate in directory.rglob("*"):
            resolved = candidate.resolve(strict=True)
            if not _within(root, resolved):
                continue
            relative = resolved.relative_to(root).as_posix()
            try:
                _deny_restricted_path(Path(relative))
            except PermissionError:
                continue
            if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(resolved.name, pattern):
                matches.append(relative)
            if len(matches) >= _MAX_RESULTS:
                break
        return {
            "workspace_id": str(payload["workspace_id"]),
            "pattern": pattern,
            "matches": sorted(matches),
        }


def create_file_inspect_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create read-only file operations."""
    service = FileInspectService(client)
    return service.invoke


def _jailed(root: Path, value: Any, *, must_exist: bool) -> Path:
    raw = Path(str(value or "").strip() or ".")
    if raw.is_absolute() or ".." in raw.parts:
        raise PermissionError("absolute or traversing paths are not accepted")
    _deny_restricted_path(raw)
    candidate = root / raw
    if must_exist:
        resolved = candidate.resolve(strict=True)
    else:
        parent = candidate.parent.resolve(strict=True)
        resolved = parent / candidate.name
    if not _within(root, resolved):
        raise PermissionError("path escapes the workspace mount")
    return resolved


def _deny_restricted_path(path: Path) -> None:
    parts = tuple(
        part.casefold()
        for part in path.parts
        if part not in {"", "."}
    )
    if any(part in _PROTECTED_PARTS for part in parts):
        raise PermissionError("protected workspace paths are not readable")
    if any(part in _SECRET_PARTS for part in parts):
        raise PermissionError("secret workspace directories are not readable")
    if not parts:
        return
    name = parts[-1]
    is_env = name == ".env" or (
        name.startswith(".env.")
        and not name.endswith(_SAFE_ENV_SUFFIXES)
    )
    if (
        is_env
        or name in _SECRET_NAMES
        or name.endswith(_SECRET_SUFFIXES)
    ):
        raise PermissionError("secret workspace files are not readable")


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _deterministic_files(
    directory: Path,
    *,
    recursive: bool,
) -> list[Path]:
    if not recursive:
        return sorted(directory.iterdir(), key=lambda item: item.name)
    result: list[Path] = []
    pending = [directory]
    excluded = {
        ".git",
        ".venv",
        "build",
        "dist",
        "node_modules",
        "target",
        "vendor",
    }
    while pending and len(result) < _MAX_RESULTS:
        current = pending.pop()
        try:
            children = sorted(
                current.iterdir(),
                key=lambda item: item.name,
                reverse=True,
            )
        except OSError:
            continue
        for child in children:
            if child.name in excluded:
                continue
            try:
                if child.is_dir() and not child.is_symlink():
                    pending.append(child)
                else:
                    result.append(child)
            except OSError:
                continue
    return sorted(result, key=lambda item: item.as_posix())[:_MAX_RESULTS]


def _git_tracked_files(
    root: Path,
    directory: Path,
    *,
    recursive: bool,
    deadline_epoch_ms: int = 0,
) -> list[Path]:
    timeout = 15.0
    if deadline_epoch_ms:
        timeout = max(
            0.001,
            min(
                timeout,
                (deadline_epoch_ms - int(time.time() * 1000)) / 1000,
            ),
        )
        if timeout <= 0.001:
            raise TimeoutError("file inspection deadline exceeded")
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached"],
            check=True,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    relative_directory = directory.relative_to(root)
    prefix = (
        ""
        if relative_directory == Path(".")
        else relative_directory.as_posix().rstrip("/") + "/"
    )
    result = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            relative = Path(raw.decode("utf-8"))
        except UnicodeDecodeError:
            continue
        if relative.is_absolute() or ".." in relative.parts:
            continue
        value = relative.as_posix()
        if prefix and not value.startswith(prefix):
            continue
        if not recursive and "/" in value[len(prefix) :]:
            continue
        candidate = root / relative
        if candidate.is_file() and not candidate.is_symlink():
            result.append(candidate)
        if len(result) >= _MAX_RESULTS:
            break
    return sorted(result, key=lambda item: item.relative_to(root).as_posix())


def _read_text_no_follow(
    root: Path,
    relative: Path,
    *,
    encoding: str,
    max_bytes: int,
    root_fd: int | None = None,
) -> tuple[str, int]:
    if relative.is_absolute() or ".." in relative.parts:
        raise PermissionError("unsafe workspace path")
    directory_fd = (
        os.dup(root_fd)
        if root_fd is not None
        else os.open(
            root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
    )
    opened: list[int] = [directory_fd]
    try:
        current_fd = directory_fd
        for part in relative.parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current_fd,
            )
            opened.append(next_fd)
            current_fd = next_fd
        file_fd = os.open(
            relative.name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=current_fd,
        )
        opened.append(file_fd)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise PermissionError("workspace path is not a regular file")
        size = file_stat.st_size
        if size > max_bytes:
            raise ValueError("file exceeds requested read budget")
        content = os.read(file_fd, max_bytes + 1)
        if len(content) > max_bytes:
            raise ValueError("file exceeds requested read budget")
        return content.decode(encoding), size
    finally:
        for descriptor in reversed(opened):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")


def _check_lifecycle(payload: Mapping[str, Any]) -> None:
    deadline = int(payload.get("_deadline_epoch_ms") or 0)
    if deadline and int(time.time() * 1000) >= deadline:
        raise TimeoutError("file inspection deadline exceeded")
    token = payload.get("_cancellation_token")
    if callable(token) and bool(token()):
        raise TimeoutError("file inspection cancelled")
    for name in ("is_cancelled", "is_set", "cancelled"):
        value = getattr(token, name, None) if token is not None else None
        if callable(value) and bool(value()):
            raise TimeoutError("file inspection cancelled")
        if value is not None and not callable(value) and bool(value):
            raise TimeoutError("file inspection cancelled")
