"""Receipt-gated local Git mutation without publication authority."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

AUTHORITY = "rumi.service.host.authorize.v1"
GIT_READ = "rumi.service.git.read.v1"
WORKSPACE = "rumi.resource.workspace.v1"
SERVICE_PACK_ID = "rumi_git_write_pack"
_BRANCH = re.compile(r"^(?![./-])(?!.*(?:\.\.|//|@\{|\\))[A-Za-z0-9._/-]{1,200}$")
_RESTRICTED_NAMES = {
    ".env", ".env.local", ".env.production", "credentials", "credentials.json",
    "id_rsa", "id_ed25519", ".npmrc", ".pypirc",
}


class GitWriteService:
    """Apply finite local Git mutations after exact receipt redemption."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Apply stage, commit, branch, or restore without network access."""
        if name not in {"stage", "commit", "branch_create", "branch_switch", "restore"}:
            raise ValueError(f"unknown Git write operation: {name}")
        arguments = _arguments(name, payload)
        root, repository = self._roots(payload)
        _assert_repository_snapshot(repository, arguments)
        self._redeem(name, payload, arguments)
        _assert_repository_snapshot(repository, arguments)
        if name == "stage":
            paths = _paths(repository, arguments["paths"], allow_missing=True)
            _git(repository, ["add", "--", *paths])
            return {"staged": paths, "published": False}
        if name == "commit":
            return self._commit(repository, arguments)
        if name == "branch_create":
            branch = _branch(arguments["branch"])
            _git(repository, ["switch", "-c", branch])
            return {"branch": branch, "created": True, "published": False}
        if name == "branch_switch":
            branch = _branch(arguments["branch"])
            _git(repository, ["switch", branch])
            return {"branch": branch, "switched": True, "published": False}
        paths = _paths(repository, arguments["paths"], allow_missing=True)
        source = str(arguments.get("source") or "").strip()
        args = ["restore"]
        if source:
            args.extend(["--source", _ref(source)])
        args.extend(["--", *paths])
        _git(repository, args)
        return {"restored": paths, "source": source or "index", "published": False}

    def _commit(
        self, repository: Path, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        paths = _paths(repository, arguments["paths"], allow_missing=True)
        message = str(arguments["message"])
        with tempfile.TemporaryDirectory(prefix="tobkiri-git-index-") as temp:
            index_path = Path(temp) / "index"
            environment = {**os.environ, "GIT_INDEX_FILE": str(index_path)}
            _git(repository, ["read-tree", arguments["expected_head"]], env=environment)
            if paths:
                _git(repository, ["add", "--", *paths], env=environment)
            elif arguments["all_tracked"]:
                tracked_changes = [
                    item
                    for item in _git(repository, ["diff", "--name-only"]).splitlines()
                    if item.strip()
                ]
                _paths(repository, tracked_changes, allow_missing=True)
                _git(repository, ["add", "-u"], env=environment)
            tree = _git(repository, ["write-tree"], env=environment).strip()
            commit_hash = _git(
                repository,
                [
                    "commit-tree",
                    tree,
                    "-p",
                    arguments["expected_head"],
                    "-m",
                    message,
                ],
                env=environment,
            ).strip()
        branch_ref = _git(repository, ["symbolic-ref", "-q", "HEAD"]).strip()
        if not branch_ref.startswith("refs/heads/"):
            raise PermissionError("Git commit requires an attached local branch")
        _git(
            repository,
            [
                "update-ref",
                branch_ref,
                commit_hash,
                arguments["expected_head"],
            ],
        )
        return {
            "commit_hash": commit_hash,
            "message": message,
            "paths": paths,
            "all_tracked": bool(arguments["all_tracked"]),
            "published": False,
        }

    def _roots(self, payload: Mapping[str, Any]) -> tuple[Path, Path]:
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
        if int(mount.get("mount_revision") or 0) != int(
            payload.get("expected_mount_revision") or -1
        ):
            raise PermissionError("workspace mount revision changed")
        root = Path(str(mount.get("root_path") or "")).resolve(strict=True)
        read = self.client.invoke(
            GIT_READ,
            "root",
            {
                "profile_id": _profile(payload),
                "workspace_id": str(payload.get("workspace_id") or ""),
            },
        )
        relative = str(read.get("repository_root") or ".")
        repository = (root / relative).resolve(strict=True)
        try:
            repository.relative_to(root)
        except ValueError as exc:
            raise PermissionError("Git repository root escapes workspace") from exc
        return root, repository

    def _redeem(
        self, name: str, payload: Mapping[str, Any], arguments: Mapping[str, Any]
    ) -> None:
        result = self.client.invoke(
            AUTHORITY,
            "redeem",
            {
                "receipt": str(payload.get("authority_receipt") or ""),
                "service_pack_id": SERVICE_PACK_ID,
                "operation": f"git.{name}",
                "authority": "git.write",
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
            raise PermissionError(str(result.get("reason") or "Git write denied"))


def create_git_write_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated local Git mutation operations."""
    return GitWriteService(client).invoke


def _arguments(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    expected_head = str(payload.get("expected_head") or "").strip()
    expected_tree = str(payload.get("expected_tree") or "").strip()
    expected_status_hash = str(payload.get("expected_status_hash") or "").strip()
    expected_mount_revision = int(payload.get("expected_mount_revision") or -1)
    if not all((expected_head, expected_tree, expected_status_hash)) or (
        expected_mount_revision < 1
    ):
        raise ValueError(
            "expected_head, expected_tree, expected_status_hash, and "
            "expected_mount_revision are required"
        )
    snapshot = {
        "expected_head": _oid(expected_head),
        "expected_tree": _oid(expected_tree),
        "expected_status_hash": _oid(expected_status_hash),
        "expected_mount_revision": expected_mount_revision,
    }
    if name in {"branch_create", "branch_switch"}:
        branch = str(payload.get("branch") or payload.get("name") or "").strip()
        if not branch:
            raise ValueError("Git branch is required")
        return {"branch": branch, **snapshot}
    paths = payload.get("paths") or payload.get("files") or []
    if not isinstance(paths, list):
        raise ValueError("Git paths must be a list")
    result: dict[str, Any] = {"paths": [str(item) for item in paths], **snapshot}
    if name == "commit":
        message = str(payload.get("message") or "").strip()
        if not message or len(message) > 10_000:
            raise ValueError("Git commit message is invalid")
        result.update(
            {"message": message, "all_tracked": bool(payload.get("all_tracked", False))}
        )
        if result["paths"] and result["all_tracked"]:
            raise ValueError("paths and all_tracked cannot be combined")
        if not result["paths"] and not result["all_tracked"]:
            raise ValueError("commit requires explicit paths or all_tracked")
    if name == "restore":
        result["source"] = str(payload.get("source") or "")
    if not result["paths"] and name in {"stage", "restore"}:
        raise ValueError("explicit Git paths are required")
    return result


def _paths(repository: Path, values: list[str], *, allow_missing: bool) -> list[str]:
    result = []
    for value in values:
        raw = Path(str(value))
        if raw.is_absolute() or ".." in raw.parts or ".git" in raw.parts:
            raise PermissionError("Git path escapes or targets metadata")
        if raw.name.casefold() in _RESTRICTED_NAMES or raw.suffix.casefold() in {".pem", ".key", ".p12"}:
            raise PermissionError("Git path is credential-sensitive")
        candidate = repository / raw
        resolved = (
            candidate.resolve(strict=True)
            if candidate.exists()
            else candidate.parent.resolve(strict=True) / candidate.name
        )
        try:
            resolved.relative_to(repository)
        except ValueError as exc:
            raise PermissionError("Git path escapes repository") from exc
        if not allow_missing and not resolved.exists():
            raise FileNotFoundError("Git path is unavailable")
        result.append(raw.as_posix())
    return result


def _oid(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", normalized):
        raise ValueError("Git snapshot digest is invalid")
    return normalized


def _assert_repository_snapshot(
    repository: Path,
    arguments: Mapping[str, Any],
) -> None:
    head = _git(repository, ["rev-parse", "HEAD"]).strip()
    tree = _git(repository, ["rev-parse", "HEAD^{tree}"]).strip()
    status = _git(
        repository,
        ["status", "--porcelain=v2", "--untracked-files=all"],
    )
    status_hash = hashlib.sha256(status.encode("utf-8")).hexdigest()
    if (
        head != arguments["expected_head"]
        or tree != arguments["expected_tree"]
        or status_hash != arguments["expected_status_hash"]
    ):
        raise PermissionError("Git repository snapshot changed")


def _git(
    repository: Path,
    args: list[str],
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=dict(env) if env is not None else None,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip() or "Git write failed")
    return completed.stdout


def _branch(value: str) -> str:
    if not _BRANCH.fullmatch(value) or value.endswith((".", "/")):
        raise ValueError("Git branch is invalid")
    return value


def _ref(value: str) -> str:
    if not value or value.startswith("-") or ".." in value or not _BRANCH.fullmatch(value):
        raise ValueError("Git source ref is invalid")
    return value


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")
