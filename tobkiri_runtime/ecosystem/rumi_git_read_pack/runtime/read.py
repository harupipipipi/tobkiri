"""Read-only Git inspection under an exact workspace mount."""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping

WORKSPACE = "rumi.resource.workspace.v1"
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@{}:+~-]{0,255}$")
_BRANCH = re.compile(r"^(?![./-])(?!.*(?:\.\.|//|@\{|\\\\))[A-Za-z0-9._/-]{1,200}$")
_REMOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_OUTPUT = 512 * 1024
_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
_MAX_SNAPSHOT_PATHS = 4_096
_MAX_SNAPSHOT_STATUS_BYTES = 8 * 1024 * 1024
_MAX_SNAPSHOT_PATH_LIST_BYTES = 8 * 1024 * 1024


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
            args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
            ref = str(payload.get("ref") or "").strip()
            if ref:
                args.append(_ref(ref))
            paths = _paths(repository, payload.get("paths"))
            if paths:
                args.extend(["--", *paths])
            output = _git(repository, args)
        elif name == "log":
            limit = max(1, min(200, int(payload.get("limit") or 20)))
            output = _git(
                repository,
                [
                    "log",
                    f"--max-count={limit}",
                    "--date=iso-strict",
                    "--format=%H%x09%aI%x09%an%x09%s",
                ],
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
                [
                    "branch",
                    "--list",
                    "--no-color",
                    "--format=%(HEAD)%09%(refname:short)",
                ],
            )
        elif name == "remote":
            output = _git(repository, ["remote", "-v"])
        elif name == "snapshot":
            return _snapshot(root, repository, payload)
        elif name == "publish_snapshot":
            return _publish_snapshot(root, repository, payload)
        elif name == "root":
            output = str(repository) + "\n"
        else:
            raise ValueError(f"unknown Git read operation: {name}")
        return {
            "workspace_id": str(payload.get("workspace_id") or ""),
            "repository_root": (
                repository.relative_to(root).as_posix() if repository != root else "."
            ),
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
        [
            "git",
            "-C",
            str(root),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "rev-parse",
            "--show-toplevel",
        ],
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
        ["git", "-C", str(repository), *_safe_git_args(args)],
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


def _git_bytes(repository: Path, args: list[str], *, input_bytes: bytes) -> bytes:
    """Run a bounded read-only Git operation with binary stdin."""

    completed = subprocess.run(
        ["git", "-C", str(repository), *_safe_git_args(args)],
        input=input_bytes,
        stdin=None,
        capture_output=True,
        timeout=30,
        check=False,
    )
    output = completed.stdout + completed.stderr
    if len(output) > _MAX_OUTPUT:
        output = output[:_MAX_OUTPUT]
    if completed.returncode != 0:
        raise RuntimeError(output.decode("utf-8", errors="replace").strip() or "Git read failed")
    return completed.stdout


def _git_digest(repository: Path, args: list[str], *, max_bytes: int) -> str:
    """Hash complete bounded Git output without the UI-output truncation."""

    digest = hashlib.sha256()
    _git_stream(repository, args, max_bytes=max_bytes, consume=digest.update)
    return digest.hexdigest()


def _git_output_bounded(
    repository: Path,
    args: list[str],
    *,
    max_bytes: int,
) -> bytes:
    """Return complete bounded machine output; fail instead of truncating it."""

    chunks: list[bytes] = []
    _git_stream(repository, args, max_bytes=max_bytes, consume=chunks.append)
    return b"".join(chunks)


def _git_stream(
    repository: Path,
    args: list[str],
    *,
    max_bytes: int,
    consume: Callable[[bytes], None],
) -> None:
    """Stream a Git result into a digest with an explicit resource ceiling."""

    process = subprocess.Popen(
        ["git", "-C", str(repository), *_safe_git_args(args)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    total = 0
    diagnostics = bytearray()
    deadline = time.monotonic() + 30
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.communicate()
                raise RuntimeError("Git snapshot timed out")
            for key, _ in selector.select(remaining):
                data = os.read(key.fd, 64 * 1024)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    total += len(data)
                    if total > max_bytes:
                        process.kill()
                        process.communicate()
                        raise ValueError("Git snapshot output exceeds maximum size")
                    consume(data)
                elif len(diagnostics) < _MAX_OUTPUT:
                    diagnostics.extend(data[: _MAX_OUTPUT - len(diagnostics)])
        if process.wait(timeout=1) != 0:
            message = diagnostics.decode("utf-8", errors="replace").strip()
            raise RuntimeError(message or "Git read failed")
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.communicate()


def _snapshot(
    root: Path,
    repository: Path,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Capture the immutable Git inputs needed by a write operation.

    The historical ``status`` digest only binds path names and status codes;
    changing a dirty file's bytes can leave it unchanged.  The additional
    worktree digest, index tree, and per-path blob entries make an approval
    receipt useful as an actual compare-and-swap precondition.
    """

    head = _git(repository, ["rev-parse", "HEAD"]).strip()
    tree = _git(repository, ["rev-parse", "HEAD^{tree}"]).strip()
    index_tree = _git(repository, ["write-tree"]).strip()
    status_hash = _status_hash(repository)
    paths = _paths(repository, payload.get("paths"))
    capture_commit = bool(payload.get("capture_commit", False))
    all_tracked = bool(payload.get("all_tracked", False))
    if capture_commit and all_tracked:
        if paths:
            raise ValueError("Git paths and all_tracked cannot be combined")
        paths = _tracked_worktree_paths(repository)
    snapshot: dict[str, Any] = {
        "workspace_id": str(payload.get("workspace_id") or ""),
        "repository_root": (repository.relative_to(root).as_posix() if repository != root else "."),
        "operation": "snapshot",
        "expected_head": head,
        "expected_tree": tree,
        "expected_index_tree": index_tree,
        "expected_status_hash": status_hash,
        "expected_worktree_hash": _worktree_hash(repository),
        "expected_path_entries": _path_entries(repository, paths),
        "read_only": True,
    }
    if capture_commit:
        snapshot["expected_head_ref"] = _symbolic_head_ref(repository)
        snapshot["expected_commit_entries"] = _captured_commit_entries(
            repository,
            paths,
        )
    source = str(payload.get("source") or "").strip()
    if source:
        snapshot["expected_restore_tree"] = _git(
            repository,
            ["rev-parse", f"{_ref(source)}^{{tree}}"],
        ).strip()
    elif paths:
        # ``git restore`` without --source uses the mutable live index.  A
        # tree object pins exactly the index state observed at approval time.
        snapshot["expected_restore_tree"] = index_tree
    branch = str(payload.get("branch") or "").strip()
    if branch:
        branch_oid = _branch_oid_or_zero(repository, branch)
        if payload.get("expect_branch_absent") and branch_oid != _zero_oid(repository):
            raise PermissionError("Git branch already exists")
        snapshot["expected_branch_oid"] = branch_oid
    return snapshot


def _publish_snapshot(
    root: Path,
    repository: Path,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Capture exact local and remote-tracking refs for publication.

    This deliberately uses only the local tracking ref.  It does not open the
    network from the read-only provider; the publisher later compares this
    OID with the actual remote through an explicit force-with-lease CAS.
    """

    remote = _remote(str(payload.get("remote") or "origin"))
    branch = _branch(str(payload.get("branch") or ""))
    remote_url = _git(repository, ["remote", "get-url", "--push", remote]).strip()
    source_oid = _local_branch_oid(repository, branch)
    tracking_ref = f"refs/remotes/{remote}/{branch}"
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "rev-parse",
            "--verify",
            "--quiet",
            tracking_ref,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode == 0:
        remote_oid = completed.stdout.strip()
    elif completed.returncode == 1:
        remote_oid = _zero_oid(repository)
    else:
        raise RuntimeError(
            (completed.stderr or completed.stdout).strip()
            or "Git remote tracking ref lookup failed"
        )
    return {
        "workspace_id": str(payload.get("workspace_id") or ""),
        "repository_root": (repository.relative_to(root).as_posix() if repository != root else "."),
        "operation": "publish_snapshot",
        "remote": remote,
        "branch": branch,
        "expected_source_oid": source_oid,
        "expected_remote_oid": remote_oid,
        "expected_remote_url": remote_url,
        "expected_remote_url_hash": hashlib.sha256(remote_url.encode("utf-8")).hexdigest(),
        "read_only": True,
    }


def _path_entries(repository: Path, paths: list[str]) -> list[dict[str, str]]:
    """Return raw blob identities without executing repository clean filters."""

    return _captured_entries(
        repository,
        paths,
        object_format=_object_hash_format(repository),
    )


def _captured_commit_entries(
    repository: Path,
    paths: list[str],
) -> list[dict[str, str]]:
    """Capture bounded raw bytes to bind a future commit receipt."""

    return _captured_entries(
        repository,
        paths,
        object_format=_object_hash_format(repository),
    )


def _captured_entries(
    repository: Path,
    paths: list[str],
    *,
    object_format: str,
) -> list[dict[str, str]]:
    """Capture stable raw path bytes while enforcing a total request budget."""

    if len(paths) > _MAX_SNAPSHOT_PATHS:
        raise ValueError("Git snapshot has too many paths")
    entries: list[dict[str, str]] = []
    total_bytes = 0
    for path in paths:
        try:
            data, is_symlink, metadata = _capture_raw_path(repository, path)
        except FileNotFoundError:
            entries.append({"path": path, "blob_oid": "", "mode": ""})
            continue
        total_bytes += len(data)
        if total_bytes > _MAX_SNAPSHOT_BYTES:
            raise ValueError("Git snapshot exceeds maximum size")
        mode = "120000" if is_symlink else ("100755" if metadata.st_mode & 0o111 else "100644")
        blob_oid = _raw_blob_oid(data, object_format=object_format)
        entries.append({"path": path, "blob_oid": blob_oid, "mode": mode})
    return entries


def _capture_raw_path(
    repository: Path,
    path: str,
) -> tuple[bytes, bool, os.stat_result]:
    """Capture one final component through verified nofollow directory FDs."""

    root_fd, parent_fd, filename = _open_verified_parent(repository, path)
    try:
        before = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            data = os.readlink(filename, dir_fd=parent_fd).encode(
                "utf-8",
                errors="surrogateescape",
            )
            after = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
            if _file_identity(before) != _file_identity(after):
                raise PermissionError("Git symlink changed while snapshotting")
            _assert_parent_chain_stable(root_fd, parent_fd, path)
            return data, True, before
        descriptor = _open_nofollow(filename, os.O_RDONLY, dir_fd=parent_fd)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise PermissionError("Git path is not a regular file")
            if opened.st_size > _MAX_SNAPSHOT_BYTES:
                raise ValueError("Git snapshot input exceeds maximum size")
            chunks: list[bytes] = []
            remaining = _MAX_SNAPSHOT_BYTES
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise ValueError("Git snapshot input exceeds maximum size")
            closed = os.fstat(descriptor)
            if _file_identity(opened) != _file_identity(closed):
                raise PermissionError("Git path changed while snapshotting")
            _assert_parent_chain_stable(root_fd, parent_fd, path)
            return b"".join(chunks), False, opened
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)
        os.close(root_fd)


def _object_hash_format(repository: Path) -> str:
    """Read the repository's object format once for raw blob hashing."""

    value = _git(repository, ["rev-parse", "--show-object-format"]).strip()
    if value not in {"sha1", "sha256"}:
        raise PermissionError("Git object format is unsupported")
    return value


def _zero_oid(repository: Path) -> str:
    """Return Git's format-correct absent-object sentinel for this repository."""

    return "0" * _object_oid_width(_object_hash_format(repository))


def _object_oid_width(object_format: str) -> int:
    """Return the object-ID width supported by one Git object format."""

    if object_format == "sha1":
        return 40
    if object_format == "sha256":
        return 64
    raise PermissionError("Git object format is unsupported")


def _raw_blob_oid(data: bytes, *, object_format: str) -> str:
    """Compute Git's raw blob object ID without attributes or child filters."""

    digest = hashlib.new(object_format)
    digest.update(f"blob {len(data)}\0".encode("ascii"))
    digest.update(data)
    return digest.hexdigest()


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return stat fields that must remain stable during one raw capture."""

    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _open_verified_parent(repository: Path, path: str) -> tuple[int, int, str]:
    """Open each ancestor from the repository dirfd without symlink traversal."""

    _require_safe_dirfd_support()
    parts = Path(path).parts
    if not parts or parts[-1] in {"", "."}:
        raise PermissionError("Git path is not a final file component")
    root_fd = _open_nofollow(repository, os.O_RDONLY | os.O_DIRECTORY)
    current = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            try:
                child = _open_nofollow(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY,
                    dir_fd=current,
                )
            except OSError as exc:
                raise PermissionError("Git path ancestor is unavailable or unsafe") from exc
            os.close(current)
            current = child
        return root_fd, current, parts[-1]
    except BaseException:
        os.close(current)
        os.close(root_fd)
        raise


def _assert_verified_parent(repository: Path, path: str) -> None:
    """Check an existing parent chain without following its final component."""

    root_fd, parent_fd, _ = _open_verified_parent(repository, path)
    try:
        _assert_parent_chain_stable(root_fd, parent_fd, path)
    finally:
        os.close(parent_fd)
        os.close(root_fd)


def _assert_parent_chain_stable(root_fd: int, parent_fd: int, path: str) -> None:
    """Reject a rename or replacement of a parent after capture began.

    The root descriptor is the workspace boundary.  Rewalking only from that
    descriptor keeps the check independent of mutable absolute path names.
    A mismatch is rejected before any captured bytes are hashed or returned.
    """

    expected = _directory_identity(os.fstat(parent_fd))
    current = os.dup(root_fd)
    try:
        for component in Path(path).parts[:-1]:
            try:
                child = _open_nofollow(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY,
                    dir_fd=current,
                )
            except OSError as exc:
                raise PermissionError("Git path ancestor changed while snapshotting") from exc
            os.close(current)
            current = child
        if _directory_identity(os.fstat(current)) != expected:
            raise PermissionError("Git path ancestor changed while snapshotting")
    finally:
        os.close(current)


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    """Return the immutable identity fields used for directory revalidation."""

    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        stat.S_IFMT(metadata.st_mode),
    )


def _require_safe_dirfd_support() -> None:
    """Fail closed when POSIX dirfd and nofollow primitives are unavailable."""

    required = (os.open, os.stat, os.readlink)
    if any(function not in os.supports_dir_fd for function in required):
        raise PermissionError("Git snapshots require POSIX dirfd support")
    if os.stat not in os.supports_follow_symlinks:
        raise PermissionError("Git snapshots require nofollow stat support")
    if getattr(os, "O_DIRECTORY", None) is None or getattr(os, "O_NOFOLLOW", None) is None:
        raise PermissionError("Git snapshots require nofollow directory support")


def _open_nofollow(
    path: str | Path,
    flags: int,
    *,
    dir_fd: int | None = None,
) -> int:
    """Open a path component only when symlink traversal is rejected."""

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise PermissionError("Git snapshots require nofollow descriptor support")
    return os.open(path, flags | nofollow, dir_fd=dir_fd)


def _worktree_hash(repository: Path) -> str:
    """Hash raw candidate bytes without asking Git to interpret worktree data."""

    digest = hashlib.sha256()
    object_format = _object_hash_format(repository)
    paths = _workspace_candidate_paths(repository)
    for entry in _captured_entries(
        repository,
        paths,
        object_format=object_format,
    ):
        _update_entry_digest(
            digest,
            entry["path"],
            entry["mode"],
            entry["blob_oid"],
        )
    return digest.hexdigest()


def _status_hash(repository: Path) -> str:
    """Bind safe index metadata without invoking `git status` or `git diff`."""

    return _git_digest(
        repository,
        ["ls-files", "--stage", "-z"],
        max_bytes=_MAX_SNAPSHOT_STATUS_BYTES,
    )


def _workspace_candidate_paths(repository: Path) -> list[str]:
    """List only paths whose raw worktree value affects a write snapshot."""

    output = _git_output_bounded(
        repository,
        [
            "ls-files",
            "--modified",
            "--deleted",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        max_bytes=_MAX_SNAPSHOT_PATH_LIST_BYTES,
    )
    paths = sorted(
        _validated_path(item, allow_restricted=True)
        for item in output.decode("utf-8", errors="surrogateescape").split("\0")
        if item
    )
    if len(paths) > _MAX_SNAPSHOT_PATHS:
        raise ValueError("Git snapshot has too many worktree changes")
    return paths


def _tracked_worktree_paths(repository: Path) -> list[str]:
    """Expand `git add -u` inputs before approval, never at effect time."""

    output = _git_output_bounded(
        repository,
        ["ls-files", "--modified", "--deleted", "-z"],
        max_bytes=_MAX_SNAPSHOT_PATH_LIST_BYTES,
    )
    paths = sorted(
        _validated_path(item, allow_restricted=True)
        for item in output.decode("utf-8", errors="surrogateescape").split("\0")
        if item
    )
    if len(paths) > _MAX_SNAPSHOT_PATHS:
        raise ValueError("Git snapshot has too many tracked changes")
    return paths


def _update_entry_digest(
    digest: Any,
    path: str,
    mode: str,
    blob_oid: str,
) -> None:
    """Frame each snapshot entry so adjacent field values cannot collide."""

    for value in (
        path.encode("utf-8", errors="surrogateescape"),
        mode.encode("ascii"),
        blob_oid.encode("ascii"),
    ):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)


def _symbolic_head_ref(repository: Path) -> str:
    """Capture the exact attached local branch to update after approval."""

    ref = _git(repository, ["symbolic-ref", "-q", "HEAD"]).strip()
    if not ref.startswith("refs/heads/"):
        raise PermissionError("Git commit requires an attached local branch")
    _branch(ref.removeprefix("refs/heads/"))
    return ref


def _local_branch_oid(repository: Path, branch: str) -> str:
    normalized = _branch(branch)
    return _git(repository, ["rev-parse", "--verify", f"refs/heads/{normalized}"]).strip()


def _branch_oid_or_zero(repository: Path, branch: str) -> str:
    """Resolve a local branch or return the explicit absent-ref sentinel."""

    normalized = _branch(branch)
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{normalized}",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode == 0:
        return completed.stdout.strip()
    if completed.returncode == 1:
        return _zero_oid(repository)
    raise RuntimeError(
        (completed.stderr or completed.stdout).strip() or "Git local branch lookup failed"
    )


def _ref(value: str) -> str:
    value = str(value or "").strip()
    if not _REF.fullmatch(value) or value.startswith("-") or ".." in value:
        raise ValueError("Git ref is invalid")
    return value


def _branch(value: str) -> str:
    normalized = str(value or "").strip()
    if not _BRANCH.fullmatch(normalized) or normalized.endswith((".", "/")):
        raise ValueError("Git branch is invalid")
    return normalized


def _remote(value: str) -> str:
    normalized = str(value or "").strip()
    if not _REMOTE.fullmatch(normalized):
        raise ValueError("Git remote name is invalid")
    return normalized


def _paths(repository: Path, value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Git paths must be a list")
    result = []
    for item in value:
        normalized = _validated_path(str(item))
        _assert_verified_parent(repository, normalized)
        result.append(normalized)
    return result


def _validated_path(value: str, *, allow_restricted: bool = False) -> str:
    """Reject traversal, Git metadata, and index-info control delimiters."""

    path = Path(str(value))
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise PermissionError("Git path escapes workspace")
    normalized = path.as_posix()
    if not normalized or any(character in normalized for character in "\x00\r\n\t"):
        raise PermissionError("Git path contains an unsafe index delimiter")
    if not allow_restricted:
        restricted = {
            ".env",
            ".env.local",
            ".env.production",
            "credentials",
            "credentials.json",
            "id_rsa",
            "id_ed25519",
            ".npmrc",
            ".pypirc",
        }
        if Path(normalized).name.casefold() in restricted or Path(normalized).suffix.casefold() in {
            ".pem",
            ".key",
            ".p12",
        }:
            raise PermissionError("Git path is credential-sensitive")
    return normalized


def _safe_git_args(args: list[str]) -> list[str]:
    """Disable repository-configured process hooks for snapshot commands."""

    return [
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        *args,
    ]
