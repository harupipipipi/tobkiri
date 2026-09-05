"""Receipt-gated exact text patches with stale-content protection."""

from __future__ import annotations

import difflib
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

AUTHORITY = "rumi.service.host.authorize.v1"
WORKSPACE = "rumi.resource.workspace.v1"
SERVICE_PACK_ID = "rumi_file_patch_pack"
_MAX_FILE_BYTES = 8 * 1024 * 1024


class FilePatchConflict(RuntimeError):
    """Raised for stale or ambiguous patch input."""


class FilePatchService:
    """Apply an exact old/new text patch under a selected workspace."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Preview or apply an exact text replacement."""
        if name not in {"preview", "apply"}:
            raise ValueError(f"unknown file patch operation: {name}")
        arguments = _arguments(payload)
        root = self._workspace(payload)
        path = _jailed(root, arguments["path"])
        before_bytes = path.read_bytes()
        if len(before_bytes) > _MAX_FILE_BYTES:
            raise ValueError("patch target exceeds size limit")
        before_hash = hashlib.sha256(before_bytes).hexdigest()
        expected = arguments["expected_sha256"]
        if expected and expected != before_hash:
            raise FilePatchConflict("patch target content hash is stale")
        encoding = arguments["encoding"]
        before = before_bytes.decode(encoding)
        count = before.count(arguments["old"])
        if count != 1:
            raise FilePatchConflict("patch old text must match exactly once")
        after = before.replace(arguments["old"], arguments["new"], 1)
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=arguments["path"],
                tofile=arguments["path"],
            )
        )
        result = {
            "path": path.relative_to(root).as_posix(),
            "before_sha256": before_hash,
            "sha256": hashlib.sha256(after.encode(encoding)).hexdigest(),
            "diff": diff,
            "patched": name == "apply",
        }
        if name == "preview":
            return result
        self._redeem(payload, arguments)
        if hashlib.sha256(path.read_bytes()).hexdigest() != before_hash:
            raise FilePatchConflict("patch target changed after authorization")
        _atomic_text(path, after, encoding)
        return result

    def _workspace(self, payload: Mapping[str, Any]) -> Path:
        mount = self.client.invoke(
            WORKSPACE,
            "get",
            {
                "profile_id": _profile(payload),
                "workspace_id": str(payload.get("workspace_id") or ""),
            },
        )
        if not isinstance(mount, Mapping):
            raise KeyError("workspace mount is unknown")
        return Path(str(mount.get("root_path") or "")).resolve(strict=True)

    def _redeem(
        self, payload: Mapping[str, Any], arguments: Mapping[str, Any]
    ) -> None:
        result = self.client.invoke(
            AUTHORITY,
            "redeem",
            {
                "receipt": str(payload.get("authority_receipt") or ""),
                "service_pack_id": SERVICE_PACK_ID,
                "operation": "file.patch",
                "authority": "file.patch",
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
            raise PermissionError(str(result.get("reason") or "file patch denied"))


def create_file_patch_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create preview and receipt-gated apply operations."""
    return FilePatchService(client).invoke


def _arguments(payload: Mapping[str, Any]) -> dict[str, Any]:
    path = str(payload.get("path") or "").strip()
    old = payload.get("old")
    new = payload.get("new")
    if not path or not isinstance(old, str) or not isinstance(new, str):
        raise ValueError("patch path, old, and new are required")
    if not old:
        raise ValueError("patch old text cannot be empty")
    return {
        "path": path,
        "old": old,
        "new": new,
        "encoding": str(payload.get("encoding") or "utf-8"),
        "expected_sha256": str(payload.get("expected_sha256") or ""),
    }


def _jailed(root: Path, value: str) -> Path:
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts:
        raise PermissionError("patch path escapes workspace")
    resolved = (root / raw).resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError("patch path escapes workspace") from exc
    if not resolved.is_file():
        raise FileNotFoundError("patch target is unavailable")
    return resolved


def _atomic_text(path: Path, value: str, encoding: str) -> None:
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")

