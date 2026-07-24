"""Read-only workspace-jailed file inspection service."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any, Callable, Mapping

WORKSPACE = "rumi.resource.workspace.v1"
_MAX_READ_BYTES = 4 * 1024 * 1024
_MAX_RESULTS = 10_000


class FileInspectService:
    """Inspect files under an exact selected workspace mount."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch one read-only file operation."""
        root = self._workspace(payload)
        if name == "read":
            return self._read(root, payload)
        if name == "stat":
            return self._stat(root, payload)
        if name == "list":
            return self._list(root, payload)
        if name == "search":
            return self._search(root, payload)
        raise ValueError(f"unknown file inspect operation: {name}")

    def _workspace(self, payload: Mapping[str, Any]) -> Path:
        workspace_id = str(payload.get("workspace_id") or "").strip()
        if not workspace_id:
            raise ValueError("workspace_id is required")
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
        return root

    def _read(self, root: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
        path = _jailed(root, payload.get("path"), must_exist=True)
        if not path.is_file():
            raise FileNotFoundError("file is unavailable")
        size = path.stat().st_size
        max_bytes = max(1, min(_MAX_READ_BYTES, int(payload.get("max_bytes") or _MAX_READ_BYTES)))
        if size > max_bytes:
            raise ValueError("file exceeds requested read budget")
        content = path.read_text(encoding=str(payload.get("encoding") or "utf-8"))
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
        iterator = directory.rglob("*") if recursive else directory.iterdir()
        items = []
        for candidate in iterator:
            resolved = candidate.resolve(strict=True)
            if not _within(root, resolved):
                continue
            stat = resolved.stat()
            items.append(
                {
                    "path": resolved.relative_to(root).as_posix(),
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
    if raw.is_absolute():
        raise PermissionError("absolute paths are not accepted")
    candidate = root / raw
    if must_exist:
        resolved = candidate.resolve(strict=True)
    else:
        parent = candidate.parent.resolve(strict=True)
        resolved = parent / candidate.name
    if not _within(root, resolved):
        raise PermissionError("path escapes the workspace mount")
    return resolved


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")

