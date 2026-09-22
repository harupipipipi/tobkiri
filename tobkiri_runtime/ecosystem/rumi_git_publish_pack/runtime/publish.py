"""Receipt-gated Git publication to an exact configured remote."""

from __future__ import annotations

import re
import hashlib
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

AUTHORITY = "rumi.service.host.authorize.v1"
GIT_READ = "rumi.service.git.read.v1"
WORKSPACE = "rumi.resource.workspace.v1"
SERVICE_PACK_ID = "rumi_git_publish_pack"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_REMOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class GitPublishService:
    """Publish one branch without owning local Git mutation or credentials."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Push or dry-run an exact remote/branch pair."""
        if name not in {"push", "dry_run"}:
            raise ValueError(f"unknown Git publish operation: {name}")
        arguments = _arguments(payload, dry_run=name == "dry_run")
        root, repository = self._roots(payload)
        _assert_local_head(repository, arguments["expected_head"])
        remote_url = _git(repository, ["remote", "get-url", arguments["remote"]]).strip()
        remote_host = _remote_host(remote_url)
        remote_url_hash = hashlib.sha256(remote_url.encode("utf-8")).hexdigest()
        if arguments["expected_remote_url_hash"] != remote_url_hash:
            raise PermissionError("Git remote URL changed or was not preflighted")
        self._redeem(name, payload, arguments)
        _assert_local_head(repository, arguments["expected_head"])
        current_url = _git(repository, ["remote", "get-url", arguments["remote"]]).strip()
        if hashlib.sha256(current_url.encode("utf-8")).hexdigest() != remote_url_hash:
            raise PermissionError("Git remote URL changed after authorization")
        args = ["push"]
        if arguments["dry_run"]:
            args.append("--dry-run")
        args.append(
            "--force-with-lease="
            f"refs/heads/{arguments['branch']}:{arguments['expected_remote_oid']}"
        )
        if arguments["set_upstream"]:
            args.append("--set-upstream")
        exact_refspec = (
            f"refs/heads/{arguments['branch']}:"
            f"refs/heads/{arguments['branch']}"
        )
        args.extend(["--", arguments["remote"], exact_refspec])
        output = _git(repository, args, timeout=180)
        return {
            "workspace_id": str(payload.get("workspace_id") or ""),
            "repository_root": repository.relative_to(root).as_posix()
            if repository != root
            else ".",
            "remote": arguments["remote"],
            "remote_host": remote_host,
            "branch": arguments["branch"],
            "force_with_lease": arguments["force_with_lease"],
            "dry_run": arguments["dry_run"],
            "published": not arguments["dry_run"],
            "output": output,
            "authority_receipt_redeemed": True,
        }

    def _roots(self, payload: Mapping[str, Any]) -> tuple[Path, Path]:
        common = {
            "profile_id": _profile(payload),
            "workspace_id": str(payload.get("workspace_id") or ""),
        }
        mount = self.client.invoke(WORKSPACE, "get", common)
        if not isinstance(mount, Mapping):
            raise KeyError("workspace mount is unknown")
        if int(mount.get("mount_revision") or 0) != int(
            payload.get("expected_mount_revision") or -1
        ):
            raise PermissionError("workspace mount revision changed")
        root = Path(str(mount.get("root_path") or "")).resolve(strict=True)
        read = self.client.invoke(GIT_READ, "root", common)
        repository = (root / str(read.get("repository_root") or ".")).resolve(strict=True)
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
                "operation": f"git.publish.{name}",
                "authority": "git.publish",
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
            raise PermissionError(str(result.get("reason") or "Git publication denied"))


def create_git_publish_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated Git publication operations."""
    return GitPublishService(client).invoke


def _arguments(payload: Mapping[str, Any], *, dry_run: bool) -> dict[str, Any]:
    remote = str(payload.get("remote") or "origin").strip()
    branch = str(payload.get("branch") or "").strip()
    if not _REMOTE.fullmatch(remote):
        raise ValueError("Git remote name is invalid")
    if not _NAME.fullmatch(branch) or branch.startswith(('-', '/')) or ".." in branch:
        raise ValueError("Git branch is invalid")
    expected_head = _oid(payload.get("expected_head"))
    expected_remote_oid = _oid(
        payload.get("expected_remote_oid"),
        allow_zero=True,
    )
    expected_mount_revision = int(payload.get("expected_mount_revision") or -1)
    if expected_mount_revision < 1:
        raise ValueError("expected_mount_revision is required")
    return {
        "remote": remote,
        "branch": branch,
        "force_with_lease": bool(payload.get("force_with_lease", False)),
        "set_upstream": bool(payload.get("set_upstream", False)),
        "dry_run": dry_run,
        "expected_remote_url_hash": str(
            payload.get("expected_remote_url_hash") or ""
        ).strip(),
        "expected_head": expected_head,
        "expected_remote_oid": expected_remote_oid,
        "expected_mount_revision": expected_mount_revision,
    }


def _oid(value: Any, *, allow_zero: bool = False) -> str:
    normalized = str(value or "").strip().lower()
    if allow_zero and normalized == "0" * 40:
        return normalized
    if not re.fullmatch(r"[0-9a-f]{40,64}", normalized):
        raise ValueError("Git object ID is invalid")
    return normalized


def _assert_local_head(repository: Path, expected_head: str) -> None:
    current = _git(repository, ["rev-parse", "HEAD"]).strip()
    if current != expected_head:
        raise PermissionError("Git local ref changed after preflight")


def _remote_host(remote_url: str) -> str:
    value = str(remote_url or "").strip()
    if value.startswith(("file:", "/", "./", "../", "ext::")):
        raise PermissionError("local and external-helper Git remotes are denied")
    if "://" in value:
        parsed = urlparse(value)
        if parsed.scheme not in {"https", "ssh"} or not parsed.hostname:
            raise PermissionError("Git remote transport is denied")
        return parsed.hostname
    if "@" in value and ":" in value:
        host = value.split("@", 1)[1].split(":", 1)[0]
        if host:
            return host
    raise PermissionError("Git remote URL is not an approved network form")


def _git(repository: Path, args: list[str], *, timeout: int = 30) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    output = (completed.stdout + completed.stderr)[:256_000]
    if completed.returncode != 0:
        raise RuntimeError(output.strip() or "Git publication failed")
    return output


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")
