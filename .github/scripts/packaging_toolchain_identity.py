"""Bind formal Git and materialize a private committed-source snapshot.

This module is intentionally rootless.  It has no sudo, installer, evaluator,
or generic subprocess entrypoint.  The only executable it starts is the exact
digest-bound Git binary, with a fixed plumbing-only command set and isolated
configuration.  Packaging Python is built by the committed builder copied into
the snapshot created here.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


APPLE_TEAM_ID = "59GAB85EFG"
APPLE_GIT_IDENTIFIER = "com.apple.git"
MACOS_SYSTEM_GIT = Path("/Library/Developer/CommandLineTools/usr/bin/git")
ISOLATED_GIT_EXEC_PATH = Path("/private/var/empty")
SOURCE_SNAPSHOT_SCHEMA = "io.tobkiri.rootless-source-snapshot.v1"
SOURCE_SNAPSHOT_MANIFEST = ".tobkiri-source-snapshot.v1.json"
SOURCE_MANIFEST = PurePosixPath(
    "tobkiri_runtime/packaged_defaultspack_source_manifest.v1.json"
)
SNAPSHOT_FILES = frozenset(
    {
        ".github/scripts/build_sealed_python_environment.py",
        ".github/scripts/prepare_tauri_resources.py",
    }
)
SNAPSHOT_PREFIXES = (
    ".github/scripts/sealed_python_sources/",
    "tobkiri_runtime/",
)
ISOLATED_GIT_ARGUMENTS = (
    "--no-optional-locks",
    "--no-replace-objects",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.attributesFile=/dev/null",
    "-c",
    "core.excludesFile=/dev/null",
    "-c",
    "diff.external=",
    "-c",
    "core.sshCommand=false",
    "-c",
    "core.pager=cat",
    "-c",
    "pager.show=cat",
)


class ToolIdentityError(ValueError):
    """Raised when formal packaging authority cannot be proved."""


@dataclass(frozen=True)
class ToolIdentity:
    """Exact executable path and SHA-256."""

    path: Path
    sha256: str


@dataclass(frozen=True)
class CodeIdentity:
    """Security-relevant macOS code-signing fields."""

    identifier: str
    team_identifier: str
    cdhash: str


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_commit(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _safe_relative(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise ToolIdentityError(f"{label} must be a string")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ToolIdentityError(f"{label} must be a safe relative path")
    return path


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_absolute(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ToolIdentityError(f"{label} path must be absolute: {path}")
    try:
        canonical = path.resolve(strict=True)
    except OSError as error:
        raise ToolIdentityError(
            f"{label} cannot be resolved: {path}: {error}"
        ) from error
    if canonical != path:
        raise ToolIdentityError(f"{label} path is not canonical: {path}")
    return path


def _regular_executable(path: Path, label: str) -> ToolIdentity:
    path = _canonical_absolute(path, label)
    before = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise ToolIdentityError(f"{label} is not a regular file: {path}")
    groups = frozenset((os.getegid(), *os.getgroups()))
    caller_writable = (
        (before.st_uid == os.geteuid() and bool(before.st_mode & stat.S_IWUSR))
        or (before.st_gid in groups and bool(before.st_mode & stat.S_IWGRP))
        or bool(before.st_mode & stat.S_IWOTH)
    )
    if not os.access(path, os.X_OK) or caller_writable:
        raise ToolIdentityError(f"{label} is not immutable and executable: {path}")
    digest = _sha256_file(path)
    after = path.lstat()
    if path.is_symlink() or _file_identity(before) != _file_identity(after):
        raise ToolIdentityError(f"{label} changed while hashed: {path}")
    return ToolIdentity(path, digest)


def _fd_has_nontrivial_acl(descriptor: int) -> bool:
    if sys.platform != "darwin":
        return False
    library = ctypes.CDLL(None, use_errno=True)
    library.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    library.acl_get_fd_np.restype = ctypes.c_void_p
    library.acl_free.argtypes = [ctypes.c_void_p]
    library.acl_free.restype = ctypes.c_int
    ctypes.set_errno(0)
    acl = library.acl_get_fd_np(descriptor, 0x00000100)
    if not acl:
        error = ctypes.get_errno()
        if error == errno.ENOENT:
            return False
        raise ToolIdentityError(f"could not inspect macOS ACL: errno={error}")
    if library.acl_free(acl) != 0:
        raise ToolIdentityError("could not release macOS ACL")
    return True


def _root_owned_path(path: Path, label: str) -> None:
    path = _canonical_absolute(path, label)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            if metadata.st_uid != 0 or metadata.st_mode & 0o022:
                raise ToolIdentityError(f"{label} has writable/non-root authority")
            if _fd_has_nontrivial_acl(descriptor):
                raise ToolIdentityError(f"{label} has nontrivial ACL authority")
    finally:
        os.close(descriptor)


def _codesign_identity(path: Path) -> CodeIdentity:
    verified = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--strict", "--all-architectures", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    details = subprocess.run(
        ["/usr/bin/codesign", "-d", "--verbose=4", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    fields: dict[str, str] = {}
    for line in details.stderr.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    cdhash = fields.get("CDHash") or fields.get("CandidateCDHash")
    if (
        verified.returncode != 0
        or details.returncode != 0
        or "adhoc" in fields.get("CodeDirectory", "")
        or not cdhash
    ):
        raise ToolIdentityError(f"unusable macOS code identity: {path}")
    return CodeIdentity(
        fields.get("Identifier", ""), fields.get("TeamIdentifier", ""), cdhash
    )


def _require_git_code_authority(path: Path) -> None:
    identity = _codesign_identity(path)
    if (
        identity.identifier != APPLE_GIT_IDENTIFIER
        or identity.team_identifier != APPLE_TEAM_ID
    ):
        raise ToolIdentityError("Git signer is not authorized")


def _resolve_git(value: str | None) -> Path:
    if value:
        return Path(value)
    if sys.platform == "darwin":
        return MACOS_SYSTEM_GIT
    discovered = shutil.which("git")
    if discovered is None:
        raise ToolIdentityError("git is unavailable for explicit binding")
    return Path(discovered).resolve(strict=True)


def bind_git(path: str | None = None) -> ToolIdentity:
    """Bind the fixed platform Git authority."""
    git = _canonical_absolute(_resolve_git(path), "Git")
    if sys.platform == "darwin":
        if git != MACOS_SYSTEM_GIT:
            raise ToolIdentityError("formal macOS Git must be the fixed system Git")
        _root_owned_path(git, "Git")
        _root_owned_path(ISOLATED_GIT_EXEC_PATH, "isolated Git environment")
        _require_git_code_authority(git)
    return _regular_executable(git, "Git")


def bind_toolchain(
    *, python: str | None = None, git: str | None = None
) -> dict[str, ToolIdentity]:
    """Bind explicit tools for non-production test callers."""
    python_path = Path(python or sys.executable).resolve(strict=True)
    return {
        "python": _regular_executable(python_path, "Python"),
        "git": _regular_executable(_resolve_git(git), "Git"),
    }


def environment_lines(identities: dict[str, ToolIdentity]) -> str:
    """Return a shell-neutral exact tool binding."""
    if set(identities) != {"python", "git"}:
        raise ToolIdentityError("toolchain identity set is incomplete")
    return (
        f"TOBKIRI_PACKAGING_PYTHON={identities['python'].path}\n"
        f"TOBKIRI_PACKAGING_PYTHON_SHA256={identities['python'].sha256}\n"
        f"TOBKIRI_PACKAGING_GIT={identities['git'].path}\n"
        f"TOBKIRI_PACKAGING_GIT_SHA256={identities['git'].sha256}\n"
    )


def write_environment_file(path: Path, payload: str) -> None:
    """Atomically replace one private regular environment file."""
    if "\r" in payload or any(line.count("=") != 1 for line in payload.splitlines()):
        raise ToolIdentityError("unsafe environment payload")
    parent = path.parent.resolve(strict=True)
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp is not None:
        try:
            parent.relative_to(Path(runner_temp).resolve(strict=True))
        except ValueError as error:
            raise ToolIdentityError("environment output escapes RUNNER_TEMP") from error
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ToolIdentityError("unsafe environment output")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def append_git_environment_file(path: Path) -> None:
    """Append formal Git to an exact private Python/source binding."""
    git = bind_git()
    git_payload = (
        f"TOBKIRI_PACKAGING_GIT={git.path}\nTOBKIRI_PACKAGING_GIT_SHA256={git.sha256}\n"
    )
    if not path.exists() and not path.is_symlink():
        write_environment_file(path, git_payload)
        return
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o077
    ):
        raise ToolIdentityError("unsafe rootless packaging environment output")
    previous = path.read_text(encoding="utf-8")
    runner_temp = os.environ.get("RUNNER_TEMP")
    required = {
        "TOBKIRI_PACKAGING_PYTHON",
        "TOBKIRI_PACKAGING_PYTHON_SHA256",
        "TOBKIRI_PACKAGING_PYTHON_SNAPSHOT",
        "TOBKIRI_PACKAGING_PYTHON_INVENTORY_SHA256",
        "TOBKIRI_SEALED_PYTHON_MANIFEST_SHA256",
        "TOBKIRI_PACKAGING_SOURCE_SNAPSHOT",
        "TOBKIRI_PACKAGING_SOURCE_TREE",
        "TOBKIRI_PACKAGING_SOURCE_INVENTORY_SHA256",
        "TOBKIRI_PACKAGING_RELEASE_DIGEST",
    }
    bindings: dict[str, str] = {}
    for line in previous.splitlines():
        if line.count("=") != 1:
            raise ToolIdentityError("malformed rootless packaging environment output")
        key, value = line.split("=", 1)
        if key not in required or key in bindings or not value or "\n" in value:
            raise ToolIdentityError("unexpected packaging environment binding")
        bindings[key] = value
    if set(bindings) != required:
        raise ToolIdentityError("incomplete rootless packaging environment output")
    for key in (
        "TOBKIRI_PACKAGING_PYTHON_SNAPSHOT",
        "TOBKIRI_PACKAGING_SOURCE_SNAPSHOT",
    ):
        snapshot = Path(bindings[key])
        if not snapshot.is_absolute():
            raise ToolIdentityError("packaging snapshot path must be absolute")
        if runner_temp is not None:
            try:
                snapshot.relative_to(Path(runner_temp).resolve(strict=True))
            except ValueError as error:
                raise ToolIdentityError(
                    "packaging snapshot escapes RUNNER_TEMP"
                ) from error
    python = Path(bindings["TOBKIRI_PACKAGING_PYTHON"])
    try:
        python.relative_to(Path(bindings["TOBKIRI_PACKAGING_PYTHON_SNAPSHOT"]))
    except ValueError as error:
        raise ToolIdentityError("packaging Python escapes its snapshot") from error
    write_environment_file(path, previous + git_payload)


def _git_environment(repository_root: Path) -> dict[str, str]:
    return {
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CEILING_DIRECTORIES": os.fspath(repository_root),
        "GIT_CONFIG": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_EXEC_PATH": os.fspath(ISOLATED_GIT_EXEC_PATH),
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": os.fspath(ISOLATED_GIT_EXEC_PATH),
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "PAGER": "cat",
        "XDG_CONFIG_HOME": os.fspath(ISOLATED_GIT_EXEC_PATH),
    }


def _git_result(
    git: ToolIdentity, repository_root: Path, *arguments: str
) -> subprocess.CompletedProcess[bytes]:
    if _regular_executable(git.path, "Git") != git:
        raise ToolIdentityError("trusted Git identity changed before execution")
    if sys.platform == "darwin":
        _root_owned_path(git.path, "Git")
        _require_git_code_authority(git.path)
    return subprocess.run(
        [git.path, *ISOLATED_GIT_ARGUMENTS, "-C", repository_root, *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=_git_environment(repository_root),
    )


def _git_output(git: ToolIdentity, repository_root: Path, *arguments: str) -> bytes:
    result = _git_result(git, repository_root, *arguments)
    if result.returncode != 0:
        raise ToolIdentityError(
            "trusted Git object read failed: " + result.stderr.decode(errors="replace")
        )
    return result.stdout


def _tree_records(
    git: ToolIdentity, root: Path, commit: str
) -> list[tuple[bytes, bytes, bytes, PurePosixPath]]:
    records = []
    for record in _git_output(
        git, root, "ls-tree", "-r", "-z", "--full-tree", commit
    ).split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, kind, object_id = header.split(b" ", 2)
            relative = _safe_relative(raw_path.decode("utf-8"), "Git tree path")
        except (UnicodeDecodeError, ValueError) as error:
            raise ToolIdentityError("trusted Git tree entry is malformed") from error
        records.append((mode, kind, object_id, relative))
    return records


def _verify_clean_checkout(git: ToolIdentity, root: Path, commit: str) -> None:
    result = _git_result(
        git,
        root,
        "diff-index",
        "--cached",
        "--quiet",
        "--no-ext-diff",
        "--no-textconv",
        "--ignore-submodules=none",
        commit,
        "--",
    )
    if result.returncode != 0:
        raise ToolIdentityError("trusted Git repository is not clean")
    for mode, kind, object_id, relative in _tree_records(git, root, commit):
        if kind != b"blob" or mode not in {b"100644", b"100755", b"120000"}:
            raise ToolIdentityError("trusted Git tree contains unsupported entry")
        path = root.joinpath(*relative.parts)
        before = path.lstat()
        if mode == b"120000":
            if not stat.S_ISLNK(before.st_mode):
                raise ToolIdentityError(f"tracked symlink type changed: {relative}")
            payload = os.fsencode(os.readlink(path))
            after = path.lstat()
        else:
            if path.is_symlink() or not stat.S_ISREG(before.st_mode):
                raise ToolIdentityError(f"tracked file type changed: {relative}")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(descriptor)
                payload = b""
                while chunk := os.read(descriptor, 1024 * 1024):
                    payload += chunk
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            if _file_identity(before) != _file_identity(opened):
                raise ToolIdentityError(f"tracked file changed before read: {relative}")
            if bool(before.st_mode & 0o111) != (mode == b"100755"):
                raise ToolIdentityError(f"tracked executable mode changed: {relative}")
        if _file_identity(before) != _file_identity(after):
            raise ToolIdentityError(f"tracked path changed while read: {relative}")
        framed = f"blob {len(payload)}\0".encode() + payload
        actual = hashlib.sha1(framed, usedforsecurity=True).hexdigest()
        if actual.encode() != object_id:
            raise ToolIdentityError(f"tracked file bytes changed: {relative}")
    if _git_output(git, root, "ls-files", "--others", "-z", "--"):
        raise ToolIdentityError("trusted Git repository has untracked paths")


def smoke_git_authority(
    git: ToolIdentity, root: Path, commit: str, committed_path: PurePosixPath
) -> None:
    """Exercise only isolated built-in Git reads."""
    head = _git_output(git, root, "rev-parse", "--verify", "HEAD^{commit}")
    if not _valid_commit(commit) or head != f"{commit}\n".encode():
        raise ToolIdentityError("trusted Git HEAD smoke mismatch")
    if not _git_output(git, root, "show", f"{commit}:{committed_path}"):
        raise ToolIdentityError("trusted Git committed blob smoke returned no bytes")
    _verify_clean_checkout(git, root, commit)


def _selected_snapshot_path(relative: PurePosixPath) -> bool:
    text = relative.as_posix()
    return text in SNAPSHOT_FILES or (
        any(text.startswith(prefix) for prefix in SNAPSHOT_PREFIXES)
        and not text.startswith("tobkiri_runtime/python-runtime/")
    )


def snapshot_committed_source(
    git: ToolIdentity, root: Path, commit: str, destination: Path
) -> tuple[str, str, str]:
    """Materialize exact committed builder inputs into a sealed private tree."""
    if not _valid_commit(commit):
        raise ToolIdentityError("source commit must be a full lowercase Git SHA")
    if (
        not destination.is_absolute()
        or destination.exists()
        or destination.is_symlink()
    ):
        raise ToolIdentityError("source snapshot destination must be new and absolute")
    destination.mkdir(mode=0o700)
    complete = False
    try:
        tree = _git_output(git, root, "rev-parse", f"{commit}^{{tree}}")
        tree_identity = tree.decode("ascii").strip()
        if not _valid_commit(tree_identity):
            raise ToolIdentityError("source tree identity is invalid")
        selected = 0
        entries: list[dict[str, object]] = []
        for mode, kind, object_id, relative in _tree_records(git, root, commit):
            if "__pycache__" in relative.parts or relative.suffix.lower() in {
                ".pyc",
                ".pyo",
            }:
                raise ToolIdentityError(
                    f"committed source contains generated Python bytecode: {relative}"
                )
            if not _selected_snapshot_path(relative):
                continue
            if kind != b"blob" or mode not in {b"100644", b"100755"}:
                raise ToolIdentityError(
                    f"unsupported source snapshot entry: {relative}"
                )
            payload = _git_output(
                git, root, "cat-file", "blob", object_id.decode("ascii")
            )
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o700 if mode == b"100755" else 0o600,
            )
            try:
                offset = 0
                while offset < len(payload):
                    offset += os.write(descriptor, payload[offset:])
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o500 if mode == b"100755" else 0o400)
            finally:
                os.close(descriptor)
            entries.append(
                {
                    "path": relative.as_posix(),
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "executable": mode == b"100755",
                }
            )
            selected += 1
        if not selected:
            raise ToolIdentityError("source snapshot selection is empty")
        manifest = destination.joinpath(*SOURCE_MANIFEST.parts)
        manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        inventory_bytes = _canonical_json(
            {
                "schema": SOURCE_SNAPSHOT_SCHEMA,
                "source_commit": commit,
                "source_tree": tree_identity,
                "source_manifest_sha256": manifest_digest,
                "files": entries,
            }
        )
        inventory_path = destination / SOURCE_SNAPSHOT_MANIFEST
        descriptor = os.open(
            inventory_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            offset = 0
            while offset < len(inventory_bytes):
                offset += os.write(descriptor, inventory_bytes[offset:])
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o400)
        finally:
            os.close(descriptor)
        inventory_digest = hashlib.sha256(inventory_bytes).hexdigest()
        for current, directories, _files in os.walk(destination, topdown=False):
            for name in directories:
                (Path(current) / name).chmod(0o500)
        destination.chmod(0o500)
        release_digest = hashlib.sha256(
            _canonical_json(
                {
                    "schema": SOURCE_SNAPSHOT_SCHEMA,
                    "source_commit": commit,
                    "source_tree": tree_identity,
                    "source_manifest_sha256": manifest_digest,
                    "source_inventory_sha256": inventory_digest,
                }
            )
        ).hexdigest()
        complete = True
        return tree_identity, release_digest, inventory_digest
    finally:
        if not complete:
            # Construction failure leaves the unpredictable private name as a
            # fail-closed residue.  Path-based recursive deletion would create
            # a name-swap deletion authority; a later run never adopts it.
            pass


def _verified_git(arguments: argparse.Namespace) -> ToolIdentity:
    if arguments.git is None or not _valid_sha256(arguments.git_sha256):
        raise ToolIdentityError("verified Git binding is required")
    git = bind_git(arguments.git)
    if git.sha256 != arguments.git_sha256:
        raise ToolIdentityError("verified Git digest mismatch")
    return git


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument("--source-commit")
    parser.add_argument("--git")
    parser.add_argument("--git-sha256")
    parser.add_argument("--smoke-git-authority", action="store_true")
    parser.add_argument("--bind-git-env", action="store_true")
    parser.add_argument("--snapshot-source", action="store_true")
    parser.add_argument("--snapshot-root", type=Path)
    parser.add_argument("--env-output", type=Path)
    raw_options = [value for value in sys.argv[1:] if value.startswith("--")]
    if any("=" in value for value in raw_options) or len(raw_options) != len(
        set(raw_options)
    ):
        parser.error("packaging action options must be unique discrete arguments")
    arguments = parser.parse_args()
    actions = [
        arguments.bind_git_env,
        arguments.snapshot_source,
        arguments.smoke_git_authority,
    ]
    if sum(actions) != 1:
        parser.error("exactly one packaging toolchain action is required")
    try:
        if arguments.bind_git_env:
            if (
                any(
                    value is not None
                    for value in (
                        arguments.repository_root,
                        arguments.provenance,
                        arguments.source_commit,
                        arguments.git,
                        arguments.git_sha256,
                        arguments.snapshot_root,
                    )
                )
                or arguments.env_output is None
            ):
                raise ToolIdentityError("invalid arguments for --bind-git-env")
            append_git_environment_file(arguments.env_output)
            return 0
        root = (arguments.repository_root or Path.cwd()).resolve(strict=True)
        git = _verified_git(arguments)
        if arguments.snapshot_source:
            if arguments.snapshot_root is None or arguments.env_output is None:
                raise ToolIdentityError("snapshot output arguments are required")
            _verify_clean_checkout(git, root, arguments.source_commit)
            tree, release_digest, inventory_digest = snapshot_committed_source(
                git, root, arguments.source_commit, arguments.snapshot_root
            )
            write_environment_file(
                arguments.env_output,
                f"TOBKIRI_PACKAGING_SOURCE_SNAPSHOT={arguments.snapshot_root}\n"
                f"TOBKIRI_PACKAGING_SOURCE_TREE={tree}\n"
                f"TOBKIRI_PACKAGING_SOURCE_INVENTORY_SHA256={inventory_digest}\n"
                f"TOBKIRI_PACKAGING_RELEASE_DIGEST={release_digest}\n",
            )
            return 0
        if arguments.provenance is None:
            raise ToolIdentityError("--provenance is required for Git smoke")
        smoke_git_authority(
            git,
            root,
            arguments.source_commit,
            _safe_relative(arguments.provenance.as_posix(), "provenance"),
        )
        return 0
    except (OSError, subprocess.SubprocessError, ToolIdentityError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
