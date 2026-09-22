"""Read-only Git inspection under an exact workspace mount."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

WORKSPACE = "rumi.resource.workspace.v1"
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@{}:+~-]{0,255}$")
_MAX_OUTPUT = 512 * 1024


class GitReadService:
    """Run a finite allowlist of nonmutating Git operations."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Inspect Git state without write or network authority."""
        root = self._workspace(payload)
        repository = _repository(root)
        if name == "status":
            output = _git(repository, ["status", "--porcelain=v2", "--branch"])
        elif name == "diff":
            args = ["diff", "--no-ext-diff", "--no-color"]
            ref = str(payload.get("ref") or "").strip()
            if ref:
                args.append(_ref(ref))
            paths = _paths(payload.get("paths"))
            if paths:
                args.extend(["--", *paths])
            output = _git(repository, args)
        elif name == "log":
            limit = max(1, min(200, int(payload.get("limit") or 20)))
            output = _git(
                repository,
                ["log", f"--max-count={limit}", "--date=iso-strict", "--format=%H%x09%aI%x09%an%x09%s"],
            )
        elif name == "show":
            ref = _ref(str(payload.get("ref") or "HEAD"))
            output = _git(
                repository,
                ["show", "--no-ext-diff", "--no-color", "--stat", "--oneline", ref],
            )
        elif name == "branch":
            output = _git(
                repository,
                ["branch", "--list", "--no-color", "--format=%(HEAD)%09%(refname:short)"],
            )
        elif name == "remote":
            output = _git(repository, ["remote", "-v"])
        elif name == "snapshot":
            head = _git(repository, ["rev-parse", "HEAD"]).strip()
            tree = _git(repository, ["rev-parse", "HEAD^{tree}"]).strip()
            status = _git(
                repository,
                ["status", "--porcelain=v2", "--untracked-files=all"],
            )
            return {
                "workspace_id": str(payload.get("workspace_id") or ""),
                "repository_root": repository.relative_to(root).as_posix()
                if repository != root
                else ".",
                "operation": name,
                "expected_head": head,
                "expected_tree": tree,
                "expected_status_hash": hashlib.sha256(
                    status.encode("utf-8")
                ).hexdigest(),
                "read_only": True,
            }
        elif name == "root":
            output = str(repository) + "\n"
        else:
            raise ValueError(f"unknown Git read operation: {name}")
        return {
            "workspace_id": str(payload.get("workspace_id") or ""),
            "repository_root": repository.relative_to(root).as_posix()
            if repository != root
            else ".",
            "operation": name,
            "output": output,
            "read_only": True,
        }

    def _workspace(self, payload: Mapping[str, Any]) -> Path:
        mount = self.client.invoke(
            WORKSPACE,
            "get",
            {
                "profile_id": str(payload.get("profile_id") or "default"),
                "workspace_id": str(payload.get("workspace_id") or ""),
            },
        )
        if not isinstance(mount, Mapping):
            raise KeyError("workspace mount is unknown")
        root = Path(str(mount.get("root_path") or "")).resolve(strict=True)
        if not root.is_dir():
            raise PermissionError("workspace root is unavailable")
        return root


def create_git_read_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create finite read-only Git operations."""
    return GitReadService(client).invoke


def _repository(root: Path) -> Path:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("workspace is not a Git repository")
    repository = Path(completed.stdout.strip()).resolve(strict=True)
    try:
        repository.relative_to(root)
    except ValueError as exc:
        raise PermissionError("Git repository root is outside workspace") from exc
    return repository


def _git(repository: Path, args: list[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    output = completed.stdout + completed.stderr
    if len(output) > _MAX_OUTPUT:
        output = output[:_MAX_OUTPUT] + b"\n[output truncated]\n"
    text = output.decode("utf-8", errors="replace")
    if completed.returncode != 0:
        raise RuntimeError(text.strip() or "Git read failed")
    return text


def _ref(value: str) -> str:
    value = str(value or "").strip()
    if not _REF.fullmatch(value) or value.startswith("-") or ".." in value:
        raise ValueError("Git ref is invalid")
    return value


def _paths(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Git paths must be a list")
    result = []
    for item in value:
        path = Path(str(item))
        if path.is_absolute() or ".." in path.parts:
            raise PermissionError("Git path escapes workspace")
        result.append(path.as_posix())
    return result
