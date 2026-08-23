"""Safe checkout isolation for coding execution attempts.

The coding surface historically called a directory snapshot a ``worktree``.
That is dangerous because a snapshot has no Git ancestry or ref semantics, and
it makes provenance claims impossible to audit.  This module keeps the three
supported modes deliberately small:

``metadata_only``
    Describes the trusted repository without creating a writable checkout.
``isolated_copy``
    Creates a bounded, non-Git file snapshot.  It is never presented as a Git
    worktree.
``git_worktree``
    Uses ``git worktree add`` with an immutable commit object and records the
    Git registry identity together with an attempt-scoped lease.

The module is intentionally independent of the agent runtime.  Callers can
use :class:`CheckoutProvisioner` from a host, a scheduler, or a test harness.
All Git commands use argument arrays and ``shell=False``; no caller input is
ever interpolated into a command string.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


CANONICAL_MODES = ("metadata_only", "isolated_copy", "git_worktree")
MODE_ALIASES = {
    "copy": "isolated_copy",
    "isolated": "isolated_copy",
    # ``worktree`` was the old public spelling.  It is intentionally mapped
    # to the real implementation, never to a copy.
    "worktree": "git_worktree",
}

ACTIVE_LEASE_STATES = frozenset({"requested", "admitted", "provisioning", "ready", "in_use", "review", "handoff", "merge_pending"})
TERMINAL_STATES = frozenset({"released", "removed", "failed", "quarantine"})
PROTECTED_CLEANUP_STATES = frozenset({"in_use", "review", "handoff", "merge_pending", "conflicted", "quarantine"})

DEFAULT_MAX_COPY_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_COPY_FILES = 20_000
DEFAULT_REGISTRY_NAME = "checkout_registry.v1.json"

_REGISTRY_LOCK_GUARD = threading.Lock()
_REGISTRY_LOCKS: dict[str, threading.RLock] = {}
_PENDING_LEASES: set[tuple[str, str]] = set()


class CheckoutIsolationError(RuntimeError):
    """Base class for fail-closed checkout errors."""


class InvalidWorkspaceMode(CheckoutIsolationError, ValueError):
    """Raised for an unknown or malformed checkout mode."""


class CheckoutSecurityError(CheckoutIsolationError, PermissionError):
    """Raised when a repository, path, or source entry is unsafe."""


class CheckoutLeaseError(CheckoutIsolationError):
    """Raised when an attempt does not own the current checkout lease."""


class CheckoutAdmissionError(CheckoutIsolationError):
    """Raised before allocation when size or capacity admission fails."""


class CheckoutLifecycleError(CheckoutIsolationError):
    """Raised for an invalid lifecycle transition or unsafe cleanup."""


def canonical_mode(value: Any, *, default: str = "metadata_only") -> str:
    """Return a canonical mode and reject ambiguous values.

    ``copy`` and ``isolated`` remain accepted for old records and clients, but
    both now mean ``isolated_copy``.  ``worktree`` means ``git_worktree`` and
    therefore requires a real Git repository at provision time.
    """

    raw = str(default if value in (None, "") else value).strip().lower()
    mode = MODE_ALIASES.get(raw, raw)
    if mode not in CANONICAL_MODES:
        raise InvalidWorkspaceMode(
            f"unsupported checkout mode {raw!r}; expected one of "
            f"{', '.join(CANONICAL_MODES)}"
        )
    return mode


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_id(value: Any, field_name: str, *, required: bool = True) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise CheckoutSecurityError(f"{field_name} is required")
    if not result:
        return ""
    if len(result) > 256 or any(ord(ch) < 0x20 for ch in result):
        raise CheckoutSecurityError(f"{field_name} is malformed")
    return result


def _path_identity(path: Path) -> dict[str, Any]:
    """Return a stable physical identity for a path without following links."""

    try:
        info = path.stat()
    except OSError as exc:
        raise CheckoutSecurityError(f"path is unavailable: {path}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise CheckoutSecurityError(f"path is not a directory: {path}")
    return {
        "path": str(path),
        "device": int(getattr(info, "st_dev", 0)),
        "inode": int(getattr(info, "st_ino", 0)),
        "mode": int(stat.S_IMODE(info.st_mode)),
    }


def _same_identity(path: Path, identity: Mapping[str, Any]) -> bool:
    try:
        current = path.stat()
    except OSError:
        return False
    return (
        int(getattr(current, "st_dev", 0)) == int(identity.get("device", -1))
        and int(getattr(current, "st_ino", 0)) == int(identity.get("inode", -1))
    )


def _canonical_existing_directory(value: str | os.PathLike[str], field_name: str) -> Path:
    if value is None or str(value).strip() == "":
        raise CheckoutSecurityError(f"{field_name} is required")
    candidate = Path(str(value)).expanduser()
    if candidate.is_symlink():
        raise CheckoutSecurityError(f"{field_name} must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise CheckoutSecurityError(f"{field_name} does not exist") from exc
    if not resolved.is_dir() or resolved.is_symlink():
        raise CheckoutSecurityError(f"{field_name} must be a real directory")
    return resolved


def _ensure_child_path(root: Path, child: Path, field_name: str = "checkout path") -> Path:
    root_resolved = root.resolve(strict=True)
    candidate = child.expanduser()
    if not candidate.is_absolute():
        candidate = root_resolved / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise CheckoutSecurityError(f"{field_name} escapes its allocation root") from exc
    if resolved == root_resolved:
        raise CheckoutSecurityError(f"{field_name} must not be the allocation root")
    # Reject a path whose existing parent chain contains a link.  The final
    # component may not exist yet, so it is checked separately by callers.
    cursor = resolved.parent
    while cursor != root_resolved:
        if cursor.is_symlink():
            raise CheckoutSecurityError(f"{field_name} traverses a symlink")
        cursor = cursor.parent
    return resolved


def _run_git(
    repository: Path,
    args: Sequence[str],
    *,
    timeout: float = 15.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run Git without shell parsing and return a text process result."""

    clean_args = [str(item) for item in args]
    if any("\x00" in item for item in clean_args):
        raise CheckoutSecurityError("Git arguments must not contain NUL bytes")
    try:
        result = subprocess.run(
            ["git", *clean_args],
            cwd=str(repository),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckoutIsolationError(f"Git command failed to start: {exc}") from exc
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout or "Git command failed").strip()
        raise CheckoutIsolationError(message[:500])
    return result


def _validate_git_ref(value: Any) -> str:
    ref = _safe_id(value, "base_ref")
    if ref.startswith("-") or any(ch in ref for ch in "\x00\r\n"):
        raise CheckoutSecurityError("base_ref is malformed")
    if ref.endswith(".") or ref.endswith("/") or ".." in ref:
        raise CheckoutSecurityError("base_ref is not a valid Git ref")
    if "@{" in ref or "\\" in ref or "//" in ref:
        raise CheckoutSecurityError("base_ref is not a valid Git ref")
    return ref


def _validate_commit(value: Any) -> str:
    commit = _safe_id(value, "base_commit")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise CheckoutSecurityError("base_commit must be a full 40-character object id")
    return commit.lower()


def repository_identity(repository: str | os.PathLike[str]) -> dict[str, Any]:
    """Resolve and attest the exact Git repository identity."""

    root = _canonical_existing_directory(repository, "repository")
    top = _run_git(root, ["rev-parse", "--show-toplevel"]).stdout.strip()
    if not top:
        raise CheckoutSecurityError("repository is not a Git checkout")
    git_root = _canonical_existing_directory(top, "Git repository root")
    if git_root != root:
        raise CheckoutSecurityError("workspace root is not the exact Git repository root")
    common_dir_raw = _run_git(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip()
    if not common_dir_raw:
        raise CheckoutSecurityError("Git common directory is unavailable")
    common_dir = Path(common_dir_raw).expanduser().resolve(strict=True)
    git_dir_raw = _run_git(root, ["rev-parse", "--path-format=absolute", "--git-dir"]).stdout.strip()
    if not git_dir_raw:
        raise CheckoutSecurityError("Git directory is unavailable")
    git_dir = Path(git_dir_raw).expanduser().resolve(strict=True)
    head = _run_git(root, ["rev-parse", "--verify", "HEAD"], check=False).stdout.strip()
    return {
        "root": str(root),
        "root_identity": _path_identity(root),
        "git_common_dir": str(common_dir),
        "git_common_identity": _path_identity(common_dir),
        "git_dir": str(git_dir),
        "head": head or None,
    }


def _resolve_base_commit(repository: Path, base_commit: Any, base_ref: Any) -> tuple[str, str | None]:
    if base_commit not in (None, ""):
        commit = _validate_commit(base_commit)
        verified = _run_git(repository, ["cat-file", "-e", f"{commit}^{{commit}}"], check=False)
        if verified.returncode != 0:
            raise CheckoutSecurityError("base_commit is not present in the trusted repository")
        return commit, None
    ref = _validate_git_ref(base_ref or "HEAD")
    resolved = _run_git(
        repository,
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        check=False,
    )
    if resolved.returncode != 0:
        raise CheckoutSecurityError("base_ref does not resolve to a commit")
    commit = resolved.stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise CheckoutSecurityError("Git returned a malformed base commit")
    return commit, ref


def _registry_lock(path: Path) -> threading.RLock:
    key = str(path.expanduser().resolve())
    with _REGISTRY_LOCK_GUARD:
        lock = _REGISTRY_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _REGISTRY_LOCKS[key] = lock
        return lock


@dataclass(frozen=True)
class CheckoutLease:
    """Attempt-scoped ownership token for one checkout."""

    lease_id: str
    attempt_id: str
    fencing_token: int
    token: str
    state: str = "requested"

    def public_dict(self) -> dict[str, Any]:
        """Return lease metadata without exposing the secret token."""

        return {
            "lease_id": self.lease_id,
            "attempt_id": self.attempt_id,
            "fencing_token": self.fencing_token,
            "state": self.state,
        }


@dataclass(frozen=True)
class CheckoutRequest:
    """Validated provisioning request.

    ``attempt_id`` is intentionally required for writable modes.  A Member or
    agent name is not an ownership substitute: concurrent attempts by the same
    Member receive separate IDs and leases.
    """

    repository: Path
    destination: Path
    allocation_root: Path | None = None
    mode: str = "metadata_only"
    attempt_id: str = ""
    workspace_id: str | None = None
    team_id: str | None = None
    work_item_id: str | None = None
    assignment_id: str | None = None
    trusted: bool = False
    repository_identity: dict[str, Any] | None = None
    allocation_identity: dict[str, Any] | None = None
    base_commit: str | None = None
    base_ref: str | None = None
    allow_paths: tuple[str, ...] = ()
    read_only: bool = False
    max_bytes: int = DEFAULT_MAX_COPY_BYTES
    max_files: int = DEFAULT_MAX_COPY_FILES

    @classmethod
    def from_values(cls, **values: Any) -> "CheckoutRequest":
        mode = canonical_mode(values.get("mode"), default="metadata_only")
        repository = _canonical_existing_directory(values.get("repository"), "repository")
        if mode == "metadata_only":
            return cls(
                repository=repository,
                destination=repository,
                allocation_root=None,
                mode=mode,
                workspace_id=_safe_id(values.get("workspace_id"), "workspace_id", required=False) or None,
                team_id=_safe_id(values.get("team_id"), "team_id", required=False) or None,
                work_item_id=_safe_id(values.get("work_item_id"), "work_item_id", required=False) or None,
                assignment_id=_safe_id(values.get("assignment_id"), "assignment_id", required=False) or None,
                trusted=bool(values.get("trusted", False)),
                repository_identity=_path_identity(repository),
                base_commit=_validate_commit(values.get("base_commit")) if values.get("base_commit") else None,
                base_ref=_validate_git_ref(values.get("base_ref")) if values.get("base_ref") else None,
            )
        allocation_root = _canonical_existing_directory(
            values.get("allocation_root") or repository.parent,
            "allocation_root",
        )
        destination_value = values.get("destination")
        if destination_value in (None, ""):
            raise CheckoutSecurityError("destination is required")
        destination = _ensure_child_path(allocation_root, Path(str(destination_value)), "destination")
        try:
            destination.relative_to(repository)
        except ValueError:
            pass
        else:
            raise CheckoutSecurityError("destination must not be inside the source repository")
        attempt_id = _safe_id(values.get("attempt_id"), "attempt_id", required=mode != "metadata_only")
        max_bytes = int(values.get("max_bytes") or DEFAULT_MAX_COPY_BYTES)
        max_files = int(values.get("max_files") or DEFAULT_MAX_COPY_FILES)
        if max_bytes <= 0 or max_files <= 0:
            raise CheckoutAdmissionError("copy limits must be positive")
        allow_paths = tuple(str(item) for item in (values.get("allow_paths") or ()))
        return cls(
            repository=repository,
            destination=destination,
            allocation_root=allocation_root,
            mode=mode,
            attempt_id=attempt_id,
            workspace_id=_safe_id(values.get("workspace_id"), "workspace_id", required=False) or None,
            team_id=_safe_id(values.get("team_id"), "team_id", required=False) or None,
            work_item_id=_safe_id(values.get("work_item_id"), "work_item_id", required=False) or None,
            assignment_id=_safe_id(values.get("assignment_id"), "assignment_id", required=False) or None,
            trusted=bool(values.get("trusted", False)),
            repository_identity=_path_identity(repository),
            allocation_identity=_path_identity(allocation_root),
            base_commit=_validate_commit(values.get("base_commit")) if values.get("base_commit") else None,
            base_ref=_validate_git_ref(values.get("base_ref")) if values.get("base_ref") else None,
            allow_paths=allow_paths,
            read_only=bool(values.get("read_only", False)),
            max_bytes=max_bytes,
            max_files=max_files,
        )


@dataclass
class CheckoutRecord:
    """Durable checkout provenance and lifecycle metadata."""

    checkout_id: str
    mode: str
    path: str | None
    repository: str
    repository_identity: dict[str, Any]
    base_commit: str | None
    base_ref: str | None
    attempt_id: str | None
    workspace_id: str | None
    team_id: str | None
    work_item_id: str | None
    assignment_id: str | None
    lease_id: str | None
    fencing_token: int | None
    lease_token_hash: str | None
    state: str
    access_mode: str = "write"
    git_registry_path: str | None = None
    git_registry_head: str | None = None
    base_manifest: dict[str, str] = field(default_factory=dict)
    excluded_paths: dict[str, str] = field(default_factory=dict)
    evidence_retaining: bool = False
    protected: bool = False
    donor: bool = False
    conflicted: bool = False
    active_processes: int = 0
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy({
            "checkout_id": self.checkout_id,
            "mode": self.mode,
            "path": self.path,
            "repository": self.repository,
            "repository_identity": self.repository_identity,
            "base_commit": self.base_commit,
            "base_ref": self.base_ref,
            "attempt_id": self.attempt_id,
            "workspace_id": self.workspace_id,
            "team_id": self.team_id,
            "work_item_id": self.work_item_id,
            "assignment_id": self.assignment_id,
            "lease_id": self.lease_id,
            "fencing_token": self.fencing_token,
            "lease_token_hash": self.lease_token_hash,
            "state": self.state,
            "access_mode": self.access_mode,
            "git_registry_path": self.git_registry_path,
            "git_registry_head": self.git_registry_head,
            "base_manifest": self.base_manifest,
            "excluded_paths": self.excluded_paths,
            "evidence_retaining": self.evidence_retaining,
            "protected": self.protected,
            "donor": self.donor,
            "conflicted": self.conflicted,
            "active_processes": self.active_processes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        })

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CheckoutRecord":
        defaults = {
            "checkout_id": "",
            "mode": "metadata_only",
            "path": None,
            "repository": "",
            "repository_identity": {},
            "base_commit": None,
            "base_ref": None,
            "attempt_id": None,
            "workspace_id": None,
            "team_id": None,
            "work_item_id": None,
            "assignment_id": None,
            "lease_id": None,
            "fencing_token": None,
            "lease_token_hash": None,
            "state": "failed",
            "access_mode": "write",
            "git_registry_path": None,
            "git_registry_head": None,
            "base_manifest": {},
            "excluded_paths": {},
            "evidence_retaining": False,
            "protected": False,
            "donor": False,
            "conflicted": False,
            "active_processes": 0,
            "created_at": _now(),
            "updated_at": _now(),
        }
        defaults.update({key: value[key] for key in defaults if key in value})
        if str(defaults["mode"]) != "legacy_isolated_copy":
            defaults["mode"] = canonical_mode(defaults["mode"])
        return cls(**defaults)


class CheckoutRegistry:
    """Small atomically-written registry for leases and checkout provenance."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()
        self._lock = _registry_lock(self.path)

    def _empty(self) -> dict[str, Any]:
        return {"schema_version": 2, "next_fencing_token": 0, "checkouts": {}}

    def read(self) -> dict[str, Any]:
        with self._lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return self._empty()
            if not isinstance(payload, dict):
                return self._empty()
            result = self._empty()
            result["next_fencing_token"] = int(payload.get("next_fencing_token") or 0)
            records = payload.get("checkouts")
            if isinstance(records, dict):
                for key, value in records.items():
                    if isinstance(value, dict):
                        try:
                            result["checkouts"][str(key)] = CheckoutRecord.from_dict(value).to_dict()
                        except (TypeError, ValueError):
                            continue
            return result

    def get(self, checkout_id: str) -> CheckoutRecord | None:
        value = self.read()["checkouts"].get(str(checkout_id))
        return CheckoutRecord.from_dict(value) if isinstance(value, dict) else None

    def list(self) -> list[CheckoutRecord]:
        return [CheckoutRecord.from_dict(value) for value in self.read()["checkouts"].values()]

    def put(self, record: CheckoutRecord) -> None:
        with self._lock:
            payload = self.read()
            payload["checkouts"][record.checkout_id] = record.to_dict()
            self._write(payload)
            if record.attempt_id:
                _PENDING_LEASES.discard(
                    (str(self.path.expanduser().resolve()), str(record.attempt_id))
                )

    def delete(self, checkout_id: str) -> None:
        with self._lock:
            payload = self.read()
            payload["checkouts"].pop(str(checkout_id), None)
            self._write(payload)

    def issue_lease(self, attempt_id: str) -> CheckoutLease:
        attempt = _safe_id(attempt_id, "attempt_id")
        pending_key = (str(self.path.expanduser().resolve()), attempt)
        with self._lock:
            payload = self.read()
            active_attempts = {
                str(item.get("attempt_id"))
                for item in payload["checkouts"].values()
                if isinstance(item, dict)
                and item.get("attempt_id") == attempt
                and item.get("state") in ACTIVE_LEASE_STATES
            }
            if active_attempts or pending_key in _PENDING_LEASES:
                raise CheckoutLeaseError("an active checkout already exists for this Execution Attempt")
            _PENDING_LEASES.add(pending_key)
            token_number = int(payload.get("next_fencing_token") or 0) + 1
            payload["next_fencing_token"] = token_number
            try:
                self._write(payload)
            except BaseException:
                _PENDING_LEASES.discard(pending_key)
                raise
        return CheckoutLease(
            lease_id=f"lease_{uuid.uuid4().hex}",
            attempt_id=attempt,
            fencing_token=token_number,
            token=secrets.token_urlsafe(32),
        )

    def _write(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            Path(temporary).replace(self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def _token_hash(lease_id: str, token: str) -> str:
    return hashlib.sha256(f"{lease_id}:{token}".encode("utf-8")).hexdigest()


_DENIED_PARTS = frozenset({
    ".git", ".hg", ".svn", ".rumi", ".rumi_snapshots", ".rumi_agents",
    "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".tox", ".cache", "cache", "caches", "build", "dist",
    "out", "target", "coverage", "user_data", "userdata", "secrets", ".aws",
    ".azure", ".docker", ".gnupg", ".kube", ".ssh",
})
_DENIED_NAMES = frozenset({
    ".env", ".npmrc", ".pypirc", ".netrc", ".git-credentials", "credentials",
    "credentials.json", "kubeconfig", "token", "tokens.json", "id_rsa",
    "id_dsa", "id_ecdsa", "id_ed25519",
})
_DENIED_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".secret", ".secrets")


def _copy_path_reason(relative: str) -> str | None:
    parts = tuple(part for part in relative.replace("\\", "/").split("/") if part and part != ".")
    if not parts or any(part == ".." for part in parts):
        return "path_escape"
    lower_parts = tuple(part.casefold() for part in parts)
    if any(part in _DENIED_PARTS for part in lower_parts):
        return "cache_or_private_data"
    name = lower_parts[-1]
    if name == ".env" or name.startswith(".env."):
        return "environment_secret"
    if name in _DENIED_NAMES or name.endswith(_DENIED_SUFFIXES):
        return "credential_or_secret"
    return None


def _normalize_relative(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip("/")
    if not text or text == "." or text.startswith("/"):
        raise CheckoutSecurityError("allowlisted path must be relative")
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise CheckoutSecurityError("allowlisted path contains traversal")
    return "/".join(parts)


def _git_tracked_paths(repository: Path) -> list[str]:
    result = _run_git(repository, ["ls-files", "--cached", "-z"])
    return [item for item in result.stdout.split("\x00") if item]


def _safe_source_path(root: Path, relative: str) -> Path:
    path = root / Path(relative)
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CheckoutSecurityError(f"source path escapes repository: {relative}") from exc
    return path


def _read_regular_file(source: Path, expected_size: int, max_bytes: int) -> bytes:
    flags = os.O_RDONLY
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(source), flags | no_follow)
    except OSError as exc:
        raise CheckoutSecurityError(f"source changed or is not a regular file: {source}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise CheckoutSecurityError(f"source changed during admission: {source}")
        if before.st_size > max_bytes:
            raise CheckoutAdmissionError(f"file exceeds copy limit: {source}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise CheckoutAdmissionError(f"file exceeds copy limit: {source}")
            chunks.append(chunk)
        after = os.fstat(fd)
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise CheckoutSecurityError(f"source changed during copy: {source}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _file_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    if not root.is_dir():
        return manifest
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest[relative] = digest
    return manifest


def _git_worktree_records(repository: Path) -> list[dict[str, str | None]]:
    result = _run_git(repository, ["worktree", "list", "--porcelain"])
    records: list[dict[str, str | None]] = []
    current: dict[str, str | None] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value
        elif key == "detached":
            current["detached"] = "true"
        elif key == "prunable":
            current["prunable"] = value
    if current:
        records.append(current)
    return records


class CheckoutProvisioner:
    """Provision and lifecycle-manage safe coding checkouts."""

    def __init__(
        self,
        *,
        registry: CheckoutRegistry | None = None,
        registry_path: str | os.PathLike[str] | None = None,
    ) -> None:
        if registry is not None and registry_path is not None:
            raise ValueError("pass registry or registry_path, not both")
        self.registry = registry or CheckoutRegistry(
            registry_path or (Path.cwd() / DEFAULT_REGISTRY_NAME)
        )

    def describe(self, repository: str | os.PathLike[str], *, workspace_id: str | None = None) -> CheckoutRecord:
        """Return metadata only; no checkout or lease is created."""

        identity = repository_identity(repository)
        root = Path(identity["root"])
        return CheckoutRecord(
            checkout_id=f"metadata_{uuid.uuid4().hex}",
            mode="metadata_only",
            path=None,
            repository=str(root),
            repository_identity=identity,
            base_commit=identity.get("head"),
            base_ref=None,
            attempt_id=None,
            workspace_id=workspace_id,
            team_id=None,
            work_item_id=None,
            assignment_id=None,
            lease_id=None,
            fencing_token=None,
            lease_token_hash=None,
            state="ready",
            access_mode="read_only",
        )

    def provision(self, request: CheckoutRequest | Mapping[str, Any]) -> tuple[CheckoutRecord, CheckoutLease | None, str | None]:
        """Provision one checkout and return its record, lease, and token.

        The returned token is only available to the caller that received the
        lease.  It is never persisted in the registry; only a hash is stored.
        """

        if not isinstance(request, CheckoutRequest):
            request = CheckoutRequest.from_values(**dict(request))
        mode = canonical_mode(request.mode)
        try:
            identity = repository_identity(request.repository)
        except CheckoutIsolationError:
            if mode not in {"metadata_only", "isolated_copy"}:
                raise
            # A bounded isolated copy may be sourced from a trusted non-Git
            # folder when the caller supplies an explicit allowlist.  It gets
            # a physical identity and a fenced lease, but never Git
            # provenance or branch/ref semantics.
            repository = _canonical_existing_directory(request.repository, "repository")
            identity = {
                "root": str(repository),
                "root_identity": _path_identity(repository),
                "git_common_dir": None,
                "git_common_identity": None,
                "git_dir": None,
                "head": None,
            }
        repository = Path(identity["root"])
        if request.repository_identity and not _same_identity(
            repository, request.repository_identity
        ):
            raise CheckoutSecurityError("repository identity changed after admission")
        if not _same_identity(repository, identity["root_identity"]):
            raise CheckoutSecurityError("repository identity changed before provision")
        if mode == "metadata_only":
            return (
                CheckoutRecord(
                    checkout_id=f"metadata_{uuid.uuid4().hex}",
                    mode=mode,
                    path=None,
                    repository=str(repository),
                    repository_identity=identity,
                    base_commit=identity.get("head"),
                    base_ref=request.base_ref,
                    attempt_id=None,
                    workspace_id=request.workspace_id,
                    team_id=request.team_id,
                    work_item_id=request.work_item_id,
                    assignment_id=request.assignment_id,
                    lease_id=None,
                    fencing_token=None,
                    lease_token_hash=None,
                    state="ready",
                    access_mode="read_only",
                ),
                None,
                None,
            )

        if not request.trusted:
            raise CheckoutSecurityError(
                "writable checkout provisioning requires an explicitly trusted repository"
            )
        if request.destination.exists() or request.destination.is_symlink():
            raise CheckoutSecurityError("destination already exists")
        if request.allocation_root and request.allocation_identity and not _same_identity(
            request.allocation_root, request.allocation_identity
        ):
            raise CheckoutSecurityError("checkout allocation root identity changed")
        commit, resolved_ref = _resolve_base_commit(repository, request.base_commit, request.base_ref)
        lease = self.registry.issue_lease(request.attempt_id)
        checkout_id = f"checkout_{uuid.uuid4().hex}"
        lease = CheckoutLease(lease.lease_id, lease.attempt_id, lease.fencing_token, lease.token, "admitted")
        record = CheckoutRecord(
            checkout_id=checkout_id,
            mode=mode,
            path=str(request.destination),
            repository=str(repository),
            repository_identity=identity,
            base_commit=commit,
            base_ref=resolved_ref,
            attempt_id=request.attempt_id,
            workspace_id=request.workspace_id,
            team_id=request.team_id,
            work_item_id=request.work_item_id,
            assignment_id=request.assignment_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            lease_token_hash=_token_hash(lease.lease_id, lease.token),
            state="provisioning",
            access_mode="read_only" if request.read_only else "write",
        )
        self.registry.put(record)
        try:
            if mode == "git_worktree":
                self._provision_git_worktree(record, request, repository, commit)
            elif mode == "isolated_copy":
                self._provision_isolated_copy(record, request, repository)
            else:  # pragma: no cover - canonical_mode makes this unreachable
                raise InvalidWorkspaceMode(mode)
            if request.read_only and mode == "git_worktree":
                self._make_read_only(Path(record.path or ""))
            record.state = "ready"
            record.updated_at = _now()
            self.registry.put(record)
            return record, lease, lease.token
        except BaseException as exc:
            record.state = "failed"
            record.updated_at = _now()
            self.registry.put(record)
            self._rollback_failed_provision(record, repository)
            if isinstance(exc, CheckoutIsolationError):
                raise
            raise CheckoutIsolationError(str(exc)) from exc

    def _provision_git_worktree(
        self,
        record: CheckoutRecord,
        request: CheckoutRequest,
        repository: Path,
        commit: str,
    ) -> None:
        destination = Path(record.path or "")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # ``git worktree add`` itself creates and registers the physical tree.
        # The commit is resolved and pinned before this call; a moving branch
        # cannot alter the resulting base.
        result = _run_git(
            repository,
            ["worktree", "add", "--detach", str(destination), commit],
            timeout=60,
        )
        if result.returncode != 0:  # pragma: no cover - _run_git(check=True)
            raise CheckoutIsolationError(result.stderr.strip())
        if not destination.is_dir() or destination.is_symlink():
            raise CheckoutSecurityError("Git did not create a real worktree directory")
        worktrees = _git_worktree_records(repository)
        destination_key = os.path.normcase(os.path.realpath(str(destination)))
        matched = next(
            (
                item
                for item in worktrees
                if item.get("path")
                and os.path.normcase(os.path.realpath(str(item["path"]))) == destination_key
            ),
            None,
        )
        if matched is None or str(matched.get("head") or "").lower() != commit.lower():
            raise CheckoutSecurityError("Git worktree registry does not match pinned base")
        record.git_registry_path = str(matched.get("path"))
        record.git_registry_head = str(matched.get("head"))

    def _provision_isolated_copy(
        self,
        record: CheckoutRecord,
        request: CheckoutRequest,
        repository: Path,
    ) -> None:
        entries, excluded, total_bytes = self._plan_copy(repository, request)
        if len(entries) > request.max_files:
            raise CheckoutAdmissionError("isolated_copy file count exceeds admission limit")
        if total_bytes > request.max_bytes:
            raise CheckoutAdmissionError("isolated_copy size exceeds admission limit")
        record.excluded_paths = excluded
        destination = Path(record.path or "")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.",
                dir=str(destination.parent),
            )
        )
        try:
            for relative, source, size in entries:
                # Re-check the root before every write, closing the common
                # source-replacement TOCTOU window.
                if not _same_identity(repository, record.repository_identity["root_identity"]):
                    raise CheckoutSecurityError("repository identity changed during isolated_copy")
                content = _read_regular_file(source, size, request.max_bytes)
                target = staging / Path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            record.base_manifest = _file_manifest(staging)
            if destination.exists() or destination.is_symlink():
                raise CheckoutSecurityError("destination appeared during isolated_copy")
            # ``Path.replace`` maps to ``MoveFileEx(REPLACE_EXISTING)`` on
            # Windows and cannot publish a directory atomically.  ``rename``
            # is a no-clobber move when the destination is absent on all
            # supported platforms, so the preflight destination check remains
            # part of the publication fence.
            staging.rename(destination)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _plan_copy(
        self,
        repository: Path,
        request: CheckoutRequest,
    ) -> tuple[list[tuple[str, Path, int]], dict[str, str], int]:
        if request.allow_paths:
            candidates = [_normalize_relative(value) for value in request.allow_paths]
        else:
            try:
                candidates = _git_tracked_paths(repository)
            except CheckoutIsolationError:
                # Non-Git sources have no tracked set.  Enumerate regular files
                # only as a bounded compatibility default; denied directories,
                # secrets, and links remain excluded by the same policy.
                candidates = [
                    path.relative_to(repository).as_posix()
                    for path in sorted(repository.rglob("*"))
                    if path.is_file() and not path.is_symlink()
                ]
        entries: list[tuple[str, Path, int]] = []
        excluded: dict[str, str] = {}
        total_bytes = 0
        seen: set[str] = set()
        for relative in candidates:
            relative = _normalize_relative(relative)
            if relative in seen:
                continue
            seen.add(relative)
            reason = _copy_path_reason(relative)
            if reason:
                excluded[relative] = reason
                continue
            source = _safe_source_path(repository, relative)
            try:
                info = source.lstat()
            except FileNotFoundError:
                if request.allow_paths:
                    raise CheckoutSecurityError(f"allowlisted path does not exist: {relative}")
                continue
            if stat.S_ISLNK(info.st_mode):
                # Git can track symlinks.  They are not copied: doing so would
                # turn a source-controlled link into an ambient path escape.
                excluded[relative] = "symlink"
                continue
            if not stat.S_ISREG(info.st_mode):
                raise CheckoutSecurityError(f"non-regular source entry is not allowed: {relative}")
            size = int(info.st_size)
            total_bytes += size
            if len(entries) >= request.max_files or total_bytes > request.max_bytes:
                raise CheckoutAdmissionError("isolated_copy admission limit exceeded")
            entries.append((relative, source, size))
        return entries, excluded, total_bytes

    @staticmethod
    def _make_read_only(path: Path) -> None:
        for child in [path, *sorted(path.rglob("*"), reverse=True)]:
            try:
                mode = stat.S_IMODE(child.lstat().st_mode)
                child.chmod(mode & ~0o222)
            except OSError:
                # Windows ACLs may not be represented by POSIX mode bits.  The
                # independent reviewer lease still prevents write operations.
                continue

    def _rollback_failed_provision(self, record: CheckoutRecord, repository: Path) -> None:
        path = Path(record.path) if record.path else None
        if record.mode == "git_worktree" and path and path.exists():
            try:
                _run_git(repository, ["worktree", "remove", "--force", str(path)], timeout=30, check=False)
            except CheckoutIsolationError:
                pass
        if path and path.exists():
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError:
                pass

    def validate_lease(
        self,
        checkout_id: str,
        *,
        attempt_id: str,
        lease_id: str,
        fencing_token: int,
        token: str,
    ) -> CheckoutRecord:
        record = self.registry.get(checkout_id)
        if record is None:
            raise CheckoutLeaseError("checkout lease is unknown")
        if (
            record.attempt_id != str(attempt_id)
            or record.lease_id != str(lease_id)
            or int(record.fencing_token or -1) != int(fencing_token)
            or record.lease_token_hash != _token_hash(str(lease_id), str(token))
        ):
            raise CheckoutLeaseError("checkout lease or fencing token is stale")
        if record.state in TERMINAL_STATES:
            raise CheckoutLeaseError("checkout lease is no longer active")
        if record.path and not Path(record.path).exists():
            raise CheckoutLeaseError("checkout path no longer exists")
        return record

    def transition(
        self,
        checkout_id: str,
        state: str,
        *,
        attempt_id: str,
        lease_id: str,
        fencing_token: int,
        token: str,
    ) -> CheckoutRecord:
        """Fenced lifecycle transition for one attempt-owned checkout."""

        record = self.validate_lease(
            checkout_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            fencing_token=fencing_token,
            token=token,
        )
        allowed = {
            "requested": {"admitted", "failed"},
            "admitted": {"provisioning", "failed"},
            "provisioning": {"ready", "failed", "quarantine"},
            "ready": {"in_use", "review", "handoff", "merge_pending", "released", "failed"},
            "in_use": {"review", "handoff", "merge_pending", "released", "quarantine"},
            "review": {"handoff", "merge_pending", "released", "quarantine"},
            "handoff": {"merge_pending", "released", "quarantine"},
            "merge_pending": {"released", "quarantine"},
            "released": {"removed", "quarantine"},
        }
        target = str(state)
        if target not in allowed.get(record.state, set()):
            raise CheckoutLifecycleError(f"invalid checkout transition {record.state!r} -> {target!r}")
        record.state = target
        record.updated_at = _now()
        self.registry.put(record)
        return record

    def release(
        self,
        checkout_id: str,
        *,
        attempt_id: str,
        lease_id: str,
        fencing_token: int,
        token: str,
    ) -> CheckoutRecord:
        """Release an attempt lease without deleting evidence or files."""

        record = self.validate_lease(
            checkout_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            fencing_token=fencing_token,
            token=token,
        )
        if record.state in PROTECTED_CLEANUP_STATES and record.active_processes:
            raise CheckoutLifecycleError("active checkout processes prevent release")
        record.state = "released"
        record.updated_at = _now()
        self.registry.put(record)
        return record

    def cleanup(
        self,
        checkout_id: str,
        *,
        attempt_id: str,
        lease_id: str,
        fencing_token: int,
        token: str,
        retain_evidence: bool = False,
    ) -> CheckoutRecord:
        """Remove only a clean, unprotected checkout owned by its attempt."""

        record = self.validate_lease(
            checkout_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            fencing_token=fencing_token,
            token=token,
        )
        if record.state not in {"ready", "released", "failed"}:
            raise CheckoutLifecycleError(f"checkout state {record.state!r} is not cleanable")
        if record.evidence_retaining or record.protected or record.donor or record.conflicted or record.active_processes:
            raise CheckoutLifecycleError("checkout is protected, dirty, conflicted, donor, or process-active")
        if record.path and Path(record.path).exists() and self._is_dirty(record):
            raise CheckoutLifecycleError("dirty checkout cannot be cleaned")
        if retain_evidence:
            record.evidence_retaining = True
            record.state = "quarantine"
            record.updated_at = _now()
            self.registry.put(record)
            return record
        path = Path(record.path) if record.path else None
        repository = Path(record.repository)
        if record.mode == "git_worktree" and path and path.exists():
            worktrees = _git_worktree_records(repository)
            key = os.path.normcase(os.path.realpath(str(path)))
            registry_item = next((item for item in worktrees if os.path.normcase(os.path.realpath(str(item.get("path") or ""))) == key), None)
            if registry_item is None or (record.git_registry_head and registry_item.get("head") != record.git_registry_head):
                raise CheckoutSecurityError("Git registry identity changed; refusing cleanup")
            _run_git(repository, ["worktree", "remove", "--force", str(path)], timeout=30)
        elif path and path.exists():
            shutil.rmtree(path)
        record.state = "removed"
        record.path = None
        record.updated_at = _now()
        self.registry.put(record)
        return record

    @staticmethod
    def _is_dirty(record: CheckoutRecord) -> bool:
        path = Path(record.path or "")
        if record.mode == "git_worktree":
            try:
                return bool(_run_git(path, ["status", "--porcelain=v2", "-z"]).stdout)
            except CheckoutIsolationError:
                return True
        return _file_manifest(path) != record.base_manifest

    def reconcile(self) -> dict[str, Any]:
        """Reconcile registry rows with Git and filesystem state.

        Missing or mismatched active checkouts are quarantined, never silently
        removed.  This makes crash recovery evidence-preserving and safe to run
        repeatedly at startup.
        """

        findings: list[dict[str, Any]] = []
        for record in self.registry.list():
            if not record.path or record.mode != "git_worktree":
                continue
            path = Path(record.path)
            try:
                registry_items = _git_worktree_records(Path(record.repository))
            except CheckoutIsolationError as exc:
                findings.append({"checkout_id": record.checkout_id, "status": "quarantine", "reason": str(exc)})
                record.state = "quarantine"
                self.registry.put(record)
                continue
            key = os.path.normcase(os.path.realpath(str(path)))
            item = next((row for row in registry_items if os.path.normcase(os.path.realpath(str(row.get("path") or ""))) == key), None)
            reason = None
            if not path.is_dir():
                reason = "physical_path_missing"
            elif item is None:
                reason = "git_registry_missing"
            elif record.git_registry_head and item.get("head") != record.git_registry_head:
                reason = "git_registry_head_changed"
            elif record.base_commit and item.get("head") != record.base_commit:
                reason = "base_commit_changed"
            if reason:
                record.state = "quarantine"
                record.updated_at = _now()
                self.registry.put(record)
                findings.append({"checkout_id": record.checkout_id, "status": "quarantine", "reason": reason})
            else:
                findings.append({"checkout_id": record.checkout_id, "status": "healthy"})
        return {"schema_version": 1, "findings": findings}

    def merge(
        self,
        checkout_id: str,
        *,
        authority: Mapping[str, Any],
        target_repository: str | os.PathLike[str],
    ) -> dict[str, Any]:
        """Perform an explicitly-authorized fast-forward merge with provenance.

        Merge is intentionally separate from completion and cleanup.  The
        authority must carry the exact attempt/lease/fencing tuple; caller
        supplied ``approved`` alone is never sufficient.
        """

        required = ("attempt_id", "lease_id", "fencing_token", "lease_token")
        if any(key not in authority for key in required):
            raise CheckoutLeaseError("exact merge authority is required")
        record = self.validate_lease(
            checkout_id,
            attempt_id=str(authority["attempt_id"]),
            lease_id=str(authority["lease_id"]),
            fencing_token=int(authority["fencing_token"]),
            token=str(authority["lease_token"]),
        )
        if record.mode != "git_worktree":
            raise CheckoutLifecycleError("only a real Git worktree can be merged")
        if record.state not in {"handoff", "merge_pending"}:
            raise CheckoutLifecycleError("checkout is not in merge-pending handoff")
        if not bool(authority.get("merge_approved")):
            raise CheckoutLeaseError("merge authority was not approved")
        source = Path(record.path or "")
        target = _canonical_existing_directory(target_repository, "target_repository")
        expected_target = Path(record.repository).resolve(strict=True)
        if target != expected_target:
            raise CheckoutSecurityError("merge target is not the exact trusted repository")
        status = _run_git(source, ["status", "--porcelain=v2", "-z"]).stdout
        if status:
            raise CheckoutLifecycleError("dirty worktree cannot be merged")
        source_head = _run_git(source, ["rev-parse", "--verify", "HEAD"]).stdout.strip().lower()
        base_commit = str(record.base_commit or "").lower()
        ancestry = _run_git(
            source,
            ["merge-base", "--is-ancestor", base_commit, source_head],
            check=False,
        )
        if ancestry.returncode != 0:
            raise CheckoutLifecycleError("worktree head is not descended from the pinned base")
        target_before = _run_git(target, ["rev-parse", "--verify", "HEAD"]).stdout.strip().lower()
        if target_before != base_commit:
            raise CheckoutLifecycleError("target moved since checkout admission")
        target_status = _run_git(target, ["status", "--porcelain=v2", "-z"]).stdout
        if target_status:
            raise CheckoutLifecycleError("dirty merge target cannot receive a checkout")
        if source_head != base_commit:
            _run_git(target, ["merge", "--ff-only", source_head], timeout=60)
        target_after = _run_git(target, ["rev-parse", "--verify", "HEAD"]).stdout.strip().lower()
        record.state = "merge_pending"
        record.updated_at = _now()
        self.registry.put(record)
        return {
            "checkout_id": record.checkout_id,
            "source_head": source_head,
            "target_head_before": target_before,
            "target_head_after": target_after,
            "repository": str(target),
            "base_commit": record.base_commit,
            "attempt_id": record.attempt_id,
            "fencing_token": record.fencing_token,
            "provenance": record.to_dict(),
        }


def migrate_checkout_record(
    record: Mapping[str, Any],
    *,
    repository: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Migrate old workspace records without manufacturing Git provenance.

    A legacy row labelled ``worktree`` is promoted to ``git_worktree`` only if
    the physical path is present in the exact repository's Git worktree
    registry and its recorded head can be attested.  Otherwise it becomes
    ``legacy_isolated_copy`` and retains an explicit non-Git provenance label.
    """

    migrated = copy.deepcopy(dict(record))
    raw_mode = str(migrated.get("mode") or migrated.get("worktree_mode") or "metadata_only").strip().lower()
    path_value = migrated.get("path") or migrated.get("workspace_root")
    is_legacy_worktree = raw_mode == "worktree"
    if raw_mode in {"copy", "isolated"}:
        migrated["mode"] = "isolated_copy"
    elif raw_mode == "worktree":
        migrated["mode"] = "legacy_isolated_copy"
    else:
        try:
            migrated["mode"] = canonical_mode(raw_mode)
        except InvalidWorkspaceMode:
            migrated["mode"] = "legacy_isolated_copy"
    migrated["migrated_at"] = _now()
    if is_legacy_worktree and repository and path_value:
        try:
            repo = _canonical_existing_directory(repository, "repository")
            path = Path(str(path_value)).expanduser().resolve(strict=True)
            identity = repository_identity(repo)
            if path == repo:
                raise CheckoutSecurityError("legacy path is the source repository root")
            rows = _git_worktree_records(repo)
            key = os.path.normcase(os.path.realpath(str(path)))
            item = next((row for row in rows if os.path.normcase(os.path.realpath(str(row.get("path") or ""))) == key), None)
            if item and item.get("head") and re.fullmatch(r"[0-9a-fA-F]{40}", str(item["head"])):
                migrated["mode"] = "git_worktree"
                migrated["repository"] = str(identity["root"])
                migrated["repository_identity"] = identity
                migrated["git_registry_path"] = str(item.get("path"))
                migrated["git_registry_head"] = str(item.get("head"))
            else:
                migrated["migration_reason"] = "legacy_worktree_not_in_git_registry"
        except (CheckoutIsolationError, OSError) as exc:
            migrated["migration_reason"] = f"legacy_worktree_attestation_failed:{type(exc).__name__}"
    if migrated.get("mode") == "legacy_isolated_copy":
        migrated["git_provenance"] = False
        migrated.setdefault("migration_reason", "legacy_copy_mode")
    return migrated


__all__ = [
    "ACTIVE_LEASE_STATES",
    "CANONICAL_MODES",
    "CheckoutAdmissionError",
    "CheckoutIsolationError",
    "CheckoutLease",
    "CheckoutLeaseError",
    "CheckoutLifecycleError",
    "CheckoutProvisioner",
    "CheckoutRecord",
    "CheckoutRegistry",
    "CheckoutRequest",
    "CheckoutSecurityError",
    "InvalidWorkspaceMode",
    "MODE_ALIASES",
    "canonical_mode",
    "migrate_checkout_record",
    "repository_identity",
]
