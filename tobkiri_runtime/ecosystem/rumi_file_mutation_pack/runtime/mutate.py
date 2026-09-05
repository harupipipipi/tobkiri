"""Receipt-gated workspace-jailed atomic file mutations."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

AUTHORITY = "rumi.service.host.authorize.v1"
WORKSPACE = "rumi.resource.workspace.v1"
SERVICE_PACK_ID = "rumi_file_mutation_pack"


class FileMutationConflict(RuntimeError):
    """Raised when expected file content is stale."""


class FileMutationService:
    """Mutate files only under an exact selected workspace mount."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Redeem authority and apply one exact file mutation."""
        if name not in {"write", "create", "delete", "move"}:
            raise ValueError(f"unknown file mutation operation: {name}")
        arguments = _arguments(name, payload)
        self._redeem(name, payload, arguments)
        root = self._workspace(payload)
        if name in {"write", "create"}:
            return self._write(root, arguments, create=name == "create")
        if name == "delete":
            return self._delete(root, arguments)
        return self._move(root, arguments)

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
        root = Path(str(mount.get("root_path") or "")).resolve(strict=True)
        if not root.is_dir():
            raise PermissionError("workspace root is unavailable")
        return root

    def _redeem(
        self,
        name: str,
        payload: Mapping[str, Any],
        arguments: Mapping[str, Any],
    ) -> None:
        result = self.client.invoke(
            AUTHORITY,
            "redeem",
            {
                "receipt": str(payload.get("authority_receipt") or ""),
                "service_pack_id": SERVICE_PACK_ID,
                "operation": f"file.{name}",
                "authority": f"file.{name}",
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
            raise PermissionError(str(result.get("reason") or "file authority denied"))

    @staticmethod
    def _write(
        root: Path, arguments: Mapping[str, Any], *, create: bool
    ) -> dict[str, Any]:
        path = _jailed(root, arguments["path"], must_exist=False)
        exists = path.exists()
        if create and exists:
            raise FileExistsError("file already exists")
        if not create and not exists:
            raise FileNotFoundError("file is unavailable")
        if exists and not path.is_file():
            raise PermissionError("target is not a file")
        before_hash = _hash_file(path) if exists else None
        _assert_expected(before_hash, arguments.get("expected_sha256"))
        encoding = str(arguments.get("encoding") or "utf-8")
        data = str(arguments.get("content") or "").encode(encoding)
        path.parent.mkdir(parents=False, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {
            "path": path.relative_to(root).as_posix(),
            "created": create,
            "written": not create,
            "size": len(data),
            "before_sha256": before_hash,
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    @staticmethod
    def _delete(root: Path, arguments: Mapping[str, Any]) -> dict[str, Any]:
        path = _jailed(root, arguments["path"], must_exist=True)
        if not path.is_file():
            raise PermissionError("only files can be deleted")
        before_hash = _hash_file(path)
        _assert_expected(before_hash, arguments.get("expected_sha256"))
        path.unlink()
        return {
            "path": path.relative_to(root).as_posix(),
            "deleted": True,
            "before_sha256": before_hash,
        }

    @staticmethod
    def _move(root: Path, arguments: Mapping[str, Any]) -> dict[str, Any]:
        source = _jailed(root, arguments["source"], must_exist=True)
        target = _jailed(root, arguments["target"], must_exist=False)
        if target.exists():
            raise FileExistsError("move target already exists")
        if not source.is_file():
            raise PermissionError("only files can be moved")
        before_hash = _hash_file(source)
        _assert_expected(before_hash, arguments.get("expected_sha256"))
        os.replace(source, target)
        return {
            "source": source.relative_to(root).as_posix(),
            "target": target.relative_to(root).as_posix(),
            "moved": True,
            "sha256": before_hash,
        }


def create_file_mutation_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated file mutation operations."""
    return FileMutationService(client).invoke


def _arguments(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if name == "move":
        source = str(payload.get("source") or "").strip()
        target = str(payload.get("target") or "").strip()
        if not source or not target:
            raise ValueError("move source and target are required")
        return {
            "source": source,
            "target": target,
            "expected_sha256": str(payload.get("expected_sha256") or ""),
        }
    path = str(payload.get("path") or "").strip()
    if not path:
        raise ValueError("file path is required")
    result = {
        "path": path,
        "expected_sha256": str(payload.get("expected_sha256") or ""),
    }
    if name in {"write", "create"}:
        if "content" not in payload:
            raise ValueError("file content is required")
        result["content"] = str(payload.get("content") or "")
        result["encoding"] = str(payload.get("encoding") or "utf-8")
    return result


def _jailed(root: Path, value: Any, *, must_exist: bool) -> Path:
    raw = Path(str(value or "").strip())
    if not str(raw) or raw.is_absolute():
        raise PermissionError("relative workspace path is required")
    candidate = root / raw
    resolved = (
        candidate.resolve(strict=True)
        if must_exist
        else candidate.parent.resolve(strict=True) / candidate.name
    )
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError("path escapes the workspace mount") from exc
    return resolved


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_expected(actual: str | None, expected: Any) -> None:
    expected = str(expected or "").strip()
    if expected and expected != actual:
        raise FileMutationConflict("file content hash is stale")


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")

