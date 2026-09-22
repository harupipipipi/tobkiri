#!/usr/bin/env python3
"""Inject one verified platform artifact into a staged Defaults v4 bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping, TypedDict

from .generator_source_manifest import (
    PROVENANCE_ERROR_FILE_REQUIRED,
    PROVENANCE_ERROR_INVALID,
    SourceProvenance,
    SourceProvenanceError,
    load_source_provenance,
    verify_source_closure,
)

ROOT = Path(__file__).resolve().parents[1]
verify_source_closure(ROOT)

from .generate_defaultspack_v4_bundle import (  # noqa: E402
    _generated_provenance,
    _normalize_pack,
    _pretty,
)
from .packaging_cleanup import (  # noqa: E402
    _is_reparse_point,
    _posix_mount_identity,
    remove_owned_path,
)
from tobkiri_protocol.canonical import canonical_digest  # noqa: E402
from tobkiri_protocol.platform_artifact import (  # noqa: E402
    artifact_digest,
    verify_platform_artifact,
)
from tobkiri_protocol.validation import validate_document  # noqa: E402


class _PublishRecord(TypedDict):
    """One directory rename and its rollback state."""

    destination: Path
    backup: Path | None
    moved: bool
    published: bool


def _normalize_relative_path(value: str, field: str) -> str:
    """Normalize a package-relative path before it is used for I/O."""
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError(f"{field} is unsafe: {value!r}")
    if value.startswith("~") or Path(value).is_absolute() or PureWindowsPath(value).is_absolute():
        raise ValueError(f"{field} is unsafe: {value!r}")
    raw_parts = value.split("/")
    if any(part == ".." for part in raw_parts):
        raise ValueError(f"{field} is unsafe: {value!r}")
    parts = [part for part in raw_parts if part not in {"", "."}]
    if not parts:
        raise ValueError(f"{field} is unsafe: {value!r}")
    return "/".join(parts)


def _reject_symlink_components(path: Path) -> None:
    """Reject symlinked path components before resolving a release path."""
    absolute = path.expanduser().absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            continue
        if current in {Path("/var"), Path("/tmp")}:
            continue
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(current, metadata):
            raise ValueError(f"release path contains a symlink: {current}")
        if current != absolute and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"release path contains a non-directory: {current}")


def _directory_identity(value: os.stat_result) -> tuple[int, int, int]:
    """Return the no-follow identity fields for one directory object."""
    return (value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))


def _require_owned_output_directory(
    path: Path,
    metadata: os.stat_result,
    field: str,
    *,
    reject_writable_group: bool = True,
) -> None:
    """Require one real, host-owned, non-world-writable output directory."""
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(path, metadata):
        raise ValueError(f"packaged {field} may not be a symlink or junction")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"packaged {field} must be a directory")
    if os.name != "nt":
        owner = getattr(metadata, "st_uid", None)
        if owner is None or owner != os.geteuid():
            raise ValueError(f"packaged {field} is not owned by the build host")
        if reject_writable_group and stat.S_IMODE(metadata.st_mode) & 0o022:
            raise ValueError(f"packaged {field} has unsafe writable permissions")


def _open_owned_output_parent(path: Path, field: str) -> tuple[int | None, object | None]:
    """Open and bind an output parent without a pathname fallback on POSIX."""
    try:
        metadata = os.lstat(path)
    except FileNotFoundError as error:
        raise ValueError(f"packaged {field} parent is missing") from error
    _require_owned_output_directory(path, metadata, f"{field} parent")
    if os.name == "nt":
        return None, None
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if not all(hasattr(os, flag) for flag in required):
        raise ValueError("packaged output requires no-follow directory descriptors")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as error:
        raise ValueError(f"packaged {field} parent could not be bound") from error
    try:
        opened = os.fstat(descriptor)
        if _directory_identity(opened) != _directory_identity(metadata):
            raise ValueError(f"packaged {field} parent changed while being bound")
        mount_identity = _posix_mount_identity(descriptor)
    except (OSError, ValueError) as error:
        os.close(descriptor)
        if isinstance(error, ValueError):
            raise
        raise ValueError(f"packaged {field} parent mount identity is unavailable") from error
    return descriptor, mount_identity


def _validate_existing_output_root(
    path: Path,
    parent_descriptor: int | None,
    parent_mount_identity: object | None,
    field: str,
) -> bool:
    """Validate an existing output root through its bound parent descriptor."""
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    _require_owned_output_directory(path, metadata, field)
    if parent_descriptor is None:
        return True
    name = path.name
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as error:
        raise ValueError(f"packaged {field} could not be opened without following links") from error
    try:
        opened = os.fstat(descriptor)
        if _directory_identity(opened) != _directory_identity(metadata):
            raise ValueError(f"packaged {field} changed while being bound")
        if (
            parent_mount_identity is not None
            and _posix_mount_identity(descriptor) != parent_mount_identity
        ):
            raise ValueError(f"packaged {field} crossed a mount boundary")
        _require_owned_output_directory(path, opened, field)
    except (OSError, ValueError) as error:
        if isinstance(error, ValueError):
            raise
        raise ValueError(f"packaged {field} identity could not be verified") from error
    finally:
        os.close(descriptor)
    return True


def _create_owned_transaction(
    parent: Path,
    parent_descriptor: int | None,
    parent_mount_identity: object | None,
) -> Path:
    """Create an unpredictable owner-only transaction leaf atomically."""
    if os.name == "nt":
        transaction = Path(
            tempfile.mkdtemp(
                prefix=".tobkiri-defaultspack-transaction-",
                dir=parent,
            )
        )
        metadata = os.lstat(transaction)
        _require_owned_output_directory(transaction, metadata, "transaction")
        return transaction
    if parent_descriptor is None:
        raise ValueError("packaged transaction requires an anchored parent descriptor")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    for _ in range(8):
        name = f".tobkiri-defaultspack-transaction-{uuid.uuid4().hex}"
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            continue
        transaction = parent / name
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
            try:
                metadata = os.fstat(descriptor)
                if _directory_identity(metadata) != (
                    metadata.st_dev,
                    metadata.st_ino,
                    stat.S_IFDIR,
                ):
                    raise ValueError("packaged transaction is not a real directory")
                _require_owned_output_directory(transaction, metadata, "transaction")
                if stat.S_IMODE(metadata.st_mode) != 0o700:
                    raise ValueError("packaged transaction has unsafe permissions")
                if (
                    parent_mount_identity is not None
                    and _posix_mount_identity(descriptor) != parent_mount_identity
                ):
                    raise ValueError("packaged transaction crossed a mount boundary")
            finally:
                os.close(descriptor)
        except (OSError, ValueError) as error:
            if isinstance(error, ValueError):
                raise
            raise ValueError("packaged transaction could not be bound") from error
        return transaction
    raise ValueError("could not allocate an unpredictable packaged transaction")


def _reject_symlinks(path: Path) -> None:
    """Reject symlinks and unsupported entries in a staged input tree."""
    if path.is_symlink():
        raise ValueError(f"release tree contains a symlink: {path}")
    if path.is_file():
        return
    if not path.is_dir():
        raise ValueError(f"release tree entry is unavailable: {path}")
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        _reject_symlinks(child)


def _path_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return the stable identity fields used by source snapshots."""
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
    )


def _snapshot_file(source: Path, destination: Path) -> None:
    """Snapshot one regular file from a no-follow descriptor."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(source), flags)
    except OSError as error:
        raise ValueError(f"release file could not be snapshotted: {source}") from error
    try:
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"release file is not regular: {source}")
            if before.st_nlink != 1:
                raise ValueError(f"release file is hard-linked: {source}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as output:
                size = 0
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    output.write(chunk)
                    size += len(chunk)
            after = os.fstat(handle.fileno())
    except OSError as error:
        raise ValueError(f"release file could not be snapshotted: {source}") from error
    if _path_identity(before) != _path_identity(after) or size != after.st_size:
        raise ValueError(f"release file changed while snapshotted: {source}")
    destination.chmod(stat.S_IMODE(after.st_mode))


def _stream_file_digest(path: Path) -> str:
    """Hash one regular staged file in bounded chunks."""
    before = path.stat(follow_symlinks=False)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"staged file is not regular: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    after = path.stat(follow_symlinks=False)
    if _path_identity(before) != _path_identity(after) or size != after.st_size:
        raise ValueError(f"staged file changed while hashed: {path}")
    return "sha256:" + digest.hexdigest()


def _snapshot_tree(source: Path, destination: Path) -> None:
    """Snapshot a complete symlink-free tree with identity checks."""
    before = source.stat(follow_symlinks=False)
    if source.is_symlink() or not stat.S_ISDIR(before.st_mode):
        raise ValueError(f"release tree is not a real directory: {source}")
    destination.mkdir(parents=True, exist_ok=False)
    destination.chmod(stat.S_IMODE(before.st_mode))
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        if child.is_symlink():
            raise ValueError(f"release tree contains a symlink: {child}")
        target = destination / child.name
        if child.is_dir():
            _snapshot_tree(child, target)
        elif child.is_file():
            _snapshot_file(child, target)
        else:
            raise ValueError(f"unsupported release tree entry: {child}")
    after = source.stat(follow_symlinks=False)
    if _path_identity(before) != _path_identity(after):
        raise ValueError(f"release tree changed while snapshotted: {source}")


def _snapshot_artifact(source: Path, destination: Path) -> Path:
    """Take the sole source artifact snapshot used by packaging."""
    if source.is_symlink() or not source.exists():
        raise ValueError("verified release artifact is unavailable or symlinked")
    if source.is_dir():
        _snapshot_tree(source, destination)
    elif source.is_file():
        _snapshot_file(source, destination)
    else:
        raise ValueError(f"unsupported release artifact: {source}")
    return destination


def _copy_tree(source: Path, destination: Path) -> None:
    """Copy a previously snapshotted tree without following links."""
    destination.mkdir(parents=True, exist_ok=False)
    destination.chmod(stat.S_IMODE(source.stat().st_mode))
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        target = destination / child.name
        if child.is_symlink():
            raise ValueError(f"staged release tree contains a symlink: {child}")
        if child.is_dir():
            _copy_tree(child, target)
        elif child.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(child, target)
            target.chmod(stat.S_IMODE(child.stat().st_mode))
        else:
            raise ValueError(f"unsupported staged release entry: {child}")


def _copy_snapshot(source: Path, destination: Path) -> None:
    """Copy one source snapshot into its final staged artifact path."""
    if source.is_dir():
        _copy_tree(source, destination)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(stat.S_IMODE(source.stat().st_mode))
    else:
        raise ValueError(f"staged release snapshot is unavailable: {source}")


def _safe_join(root: Path, relative: str, field: str) -> Path:
    """Join one normalized path only when it remains under its root."""
    normalized = _normalize_relative_path(relative, field)
    root = root.expanduser().absolute()
    _reject_symlink_components(root)
    if root.is_symlink():
        raise ValueError(f"{field} root may not be a symlink: {root}")
    candidate = root.joinpath(*normalized.split("/"))
    _reject_symlink_components(candidate.parent)
    if candidate.is_symlink():
        raise ValueError(f"{field} contains a symlink: {candidate}")
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as error:
        raise ValueError(f"{field} escapes its root: {relative}") from error
    return candidate


def _entrypoint_path(artifact_root: Path, entrypoint: str) -> Path:
    """Return a normalized regular entrypoint inside the artifact root."""
    path = _safe_join(artifact_root, entrypoint, "packaged entrypoint")
    try:
        path.resolve(strict=True).relative_to(artifact_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError("packaged entrypoint escapes its artifact root") from exc
    if path.is_symlink() or not path.is_file():
        raise ValueError("packaged entrypoint must be a regular file")
    return path


def _read_bounded_header(path: Path) -> tuple[bytes, bytes | None]:
    """Read only the bounded header needed for architecture validation."""
    before = path.stat(follow_symlinks=False)
    if path.is_symlink() or not path.is_file():
        raise ValueError("entrypoint is not a regular file")
    with path.open("rb") as handle:
        prefix = handle.read(64)
        pe_header: bytes | None = None
        if prefix[:2] == b"MZ" and len(prefix) >= 64:
            pe_offset = int.from_bytes(prefix[60:64], "little")
            if pe_offset < 64 or pe_offset > before.st_size - 24:
                raise ValueError("PE entrypoint header is out of bounds")
            handle.seek(pe_offset)
            pe_header = handle.read(24)
            if len(pe_header) < 24 or pe_header[:4] != b"PE\0\0":
                raise ValueError("PE entrypoint signature is invalid or truncated")
    after = path.stat(follow_symlinks=False)
    if _path_identity(before) != _path_identity(after):
        raise ValueError("entrypoint changed while its header was verified")
    return prefix, pe_header


def _validate_binary_architecture(entrypoint: Path, architecture: str) -> None:
    """Validate recognized binary headers without accepting malformed PE data."""
    payload, pe_header = _read_bounded_header(entrypoint)
    actual: str | None = None
    if payload[:4] in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"}:
        if len(payload) < 8:
            raise ValueError("Mach-O entrypoint header is truncated")
        machine = int.from_bytes(
            payload[4:8],
            "little" if payload[:4] == b"\xcf\xfa\xed\xfe" else "big",
        )
        actual = {0x01000007: "x86_64", 0x0100000C: "arm64"}.get(machine)
    elif payload[:4] == b"\x7fELF":
        if len(payload) < 20:
            raise ValueError("ELF entrypoint header is truncated")
        machine = int.from_bytes(
            payload[18:20], "little" if payload[5:6] == b"\x01" else "big"
        )
        actual = {62: "x86_64", 183: "arm64"}.get(machine)
    elif payload[:2] == b"MZ":
        if pe_header is None:
            raise ValueError("PE entrypoint header is truncated")
        actual = {0x8664: "x86_64", 0xAA64: "arm64"}.get(
            int.from_bytes(pe_header[4:6], "little")
        )
    if actual is not None and actual != architecture:
        raise ValueError(
            f"entrypoint architecture does not match target: expected {architecture}, got {actual}"
        )


def _new_transaction(bundle_root: Path, artifact_root: Path) -> Path:
    """Bind caller-owned roots and create one private transaction leaf."""
    if bundle_root == artifact_root:
        raise ValueError("bundle and artifact roots must be distinct")
    if bundle_root.is_relative_to(artifact_root) or artifact_root.is_relative_to(bundle_root):
        raise ValueError("bundle and artifact roots must not overlap")
    _reject_symlink_components(bundle_root)
    _reject_symlink_components(artifact_root)
    bundle_parent_descriptor, bundle_parent_mount = _open_owned_output_parent(
        bundle_root.parent, "bundle"
    )
    artifact_parent_descriptor, artifact_parent_mount = _open_owned_output_parent(
        artifact_root.parent, "artifact"
    )
    try:
        if (
            bundle_parent_mount is not None
            and artifact_parent_mount is not None
            and bundle_parent_mount != artifact_parent_mount
        ):
            raise ValueError("bundle and artifact outputs must share one filesystem")
        if not _validate_existing_output_root(
            bundle_root,
            bundle_parent_descriptor,
            bundle_parent_mount,
            "bundle root",
        ):
            raise ValueError("packaged bundle root must be a real directory")
        _validate_existing_output_root(
            artifact_root,
            artifact_parent_descriptor,
            artifact_parent_mount,
            "artifact root",
        )
        return _create_owned_transaction(
            bundle_root.parent,
            bundle_parent_descriptor,
            bundle_parent_mount,
        )
    finally:
        if bundle_parent_descriptor is not None:
            os.close(bundle_parent_descriptor)
        if artifact_parent_descriptor is not None:
            os.close(artifact_parent_descriptor)


def _remove_owned(path: Path) -> None:
    """Remove only a transaction or rollback path owned by this operation."""
    remove_owned_path(
        path,
        owner_root=path.parent,
        operation="remove packaged Defaults transaction or rollback path",
    )


def _publish_directories(
    staged_bundle: Path,
    bundle_root: Path,
    staged_artifacts: Path,
    artifact_root: Path,
) -> None:
    """Publish both roots with rollback if any rename fails."""
    records: list[_PublishRecord] = []
    try:
        for staged, destination in (
            (staged_bundle, bundle_root),
            (staged_artifacts, artifact_root),
        ):
            moved = False
            backup: Path | None = None
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_dir():
                    raise ValueError(f"publish destination must be a directory: {destination}")
                backup = Path(
                    tempfile.mkdtemp(
                        prefix=f".{destination.name}.rollback-", dir=destination.parent
                    )
                )
                _remove_owned(backup)
                os.replace(destination, backup)
                moved = True
            record: _PublishRecord = {
                "destination": destination,
                "backup": backup,
                "moved": moved,
                "published": False,
            }
            records.append(record)
            os.replace(staged, destination)
            record["published"] = True
    except Exception:
        for record in reversed(records):
            destination = record["destination"]
            if record["published"] and (
                destination.exists() or destination.is_symlink()
            ):
                _remove_owned(destination)
            backup = record["backup"]
            if record["moved"] and backup is not None and backup.exists():
                os.replace(backup, destination)
        raise
    finally:
        for record in records:
            backup = record["backup"]
            if backup is not None and (backup.exists() or backup.is_symlink()):
                _remove_owned(backup)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    """Read one staged JSON object without following a symlink."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is invalid: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write deterministic owner-readable JSON only into staging."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_pretty(dict(value)))
    path.chmod(0o600)


def _preverified_source_provenance(
    provenance_file: str | Path | None,
) -> SourceProvenance:
    """Accept only the core-bound provenance file inside a sealed snapshot."""
    if provenance_file is None:
        raise SourceProvenanceError(
            PROVENANCE_ERROR_FILE_REQUIRED,
            "source provenance file is required",
            context="packaged Profile source provenance is invalid",
        )
    try:
        candidate = Path(provenance_file).expanduser()
        source_root = candidate.parent if candidate.is_absolute() else ROOT
        return load_source_provenance(source_root, candidate)
    except SourceProvenanceError as error:
        raise SourceProvenanceError(
            error.code,
            error.reason,
            context="packaged Profile source provenance is invalid",
        ) from None
    except (OSError, TypeError, ValueError):
        raise SourceProvenanceError(
            PROVENANCE_ERROR_INVALID,
            "source provenance could not be verified",
            context="packaged Profile source provenance is invalid",
        ) from None


def _validate_staged_bundle(
    bundle_root: Path,
    artifact_root: Path,
    relative_path: str,
    entrypoint: str,
    platform: str,
    architecture: str,
    bundle_identity: str,
    digest: str,
    entrypoint_digest: str,
) -> None:
    """Verify every staged Pack/Profile/Shell/lock byte before publication."""
    shell_path = bundle_root / "shell.tauri.default.shell.v1.json"
    profile_path = bundle_root / "defaults.profile.v4.json"
    lock_path = bundle_root / "bundle.lock.json"
    shell = validate_document(_read_json(shell_path, "Shell"), "shell")
    profile = validate_document(_read_json(profile_path, "Profile"), "profile")
    lock = _read_json(lock_path, "bundle lock")
    if lock.get("schema") != "io.tobkiri.defaultspack-bundle-lock.v1":
        raise ValueError("bundle lock schema is invalid")
    entries = lock.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("bundle lock has no entries")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("bundle lock contains a malformed entry")
        path_value = entry.get("path")
        kind = entry.get("kind")
        if not isinstance(path_value, str) or not isinstance(kind, str):
            raise ValueError("bundle lock entry path/kind is invalid")
        normalized = _normalize_relative_path(path_value, "bundle lock path")
        if normalized in seen or normalized != path_value:
            raise ValueError("bundle lock paths must be unique canonical relatives")
        seen.add(normalized)
        path = _safe_join(bundle_root, normalized, "bundle lock path")
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"bundle lock entry is missing or symlinked: {path}")
        actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != entry.get("digest"):
            raise ValueError(f"bundle lock digest mismatch: {normalized}")
        document = _read_json(path, f"bundle {kind}")
        if kind in {"pack", "base", "shell", "profile", "executable_catalog"}:
            validate_document(document, kind)
        else:
            raise ValueError(f"bundle lock kind is unsupported: {kind}")

    selected = _safe_join(artifact_root, relative_path, "packaged artifact path")
    selected_resolved = selected.resolve(strict=True)
    entry = _entrypoint_path(artifact_root, entrypoint)
    try:
        entry.resolve(strict=True).relative_to(selected_resolved)
    except (OSError, ValueError) as error:
        raise ValueError("packaged entrypoint is outside the selected artifact") from error
    variant = {
        "platform": platform,
        "architecture": architecture,
        "artifact_digest": digest,
        "entrypoint_digest": entrypoint_digest,
        "relative_path": relative_path,
        "entrypoint": entrypoint,
        "bundle_identity": bundle_identity,
    }
    _validate_binary_architecture(entry, architecture)
    verify_platform_artifact(artifact_root, variant)
    variants = shell.get("launch", {}).get("variants")
    if not isinstance(variants, list) or len(variants) != 1 or variants[0] != variant:
        raise ValueError("staged Shell variant does not match the selected artifact")
    if profile.get("shell", {}).get("platform") != platform or profile.get("shell", {}).get(
        "architecture"
    ) != architecture:
        raise ValueError("staged Profile target does not match the selected artifact")
    if profile.get("shell", {}).get("artifact_digest") != digest:
        raise ValueError("staged Profile artifact digest does not match the Shell")
    if profile.get("shell", {}).get("executable_artifact_digest") != entrypoint_digest:
        raise ValueError("staged Profile entrypoint digest does not match the Shell")


def _package_transaction(
    *,
    source_artifact: Path | None,
    bundle_root: Path,
    artifact_root: Path,
    relative_path: str,
    entrypoint: str,
    platform: str,
    architecture: str,
    bundle_identity: str,
    source_provenance_file: str | Path | None,
) -> None:
    """Build both output roots fully in one same-filesystem transaction."""
    relative_path = _normalize_relative_path(relative_path, "packaged artifact path")
    entrypoint = _normalize_relative_path(entrypoint, "packaged entrypoint")
    provenance = _preverified_source_provenance(source_provenance_file)
    commit = provenance.source_commit
    bundle_root = bundle_root.expanduser().absolute()
    artifact_root = artifact_root.expanduser().absolute()
    transaction: Path | None = _new_transaction(bundle_root, artifact_root)
    try:
        assert transaction is not None
        staged_bundle = transaction / "bundle"
        staged_artifacts = transaction / "artifacts"
        _snapshot_tree(bundle_root, staged_bundle)
        if artifact_root.exists():
            _snapshot_tree(artifact_root, staged_artifacts)
        else:
            staged_artifacts.mkdir(parents=True, exist_ok=False)

        if source_artifact is not None:
            source_input = source_artifact.expanduser().absolute()
            _reject_symlink_components(source_input)
            if source_input.is_symlink():
                raise ValueError("verified release artifact is unavailable or symlinked")
            source = source_input.resolve(strict=True)
            source_snapshot = transaction / "source-snapshot" / source.name
            _snapshot_artifact(source, source_snapshot)
            destination = _safe_join(staged_artifacts, relative_path, "packaged artifact path")
            if destination.exists() or destination.is_symlink():
                raise ValueError("packaged Profile artifact destination already exists")
            _copy_snapshot(source_snapshot, destination)

        selected = _safe_join(staged_artifacts, relative_path, "packaged artifact path")
        digest = artifact_digest(selected)
        entrypoint_path = _entrypoint_path(staged_artifacts, entrypoint)
        try:
            entrypoint_path.resolve(strict=True).relative_to(selected.resolve(strict=True))
        except (OSError, ValueError) as error:
            raise ValueError("packaged entrypoint is outside the selected artifact") from error
        entrypoint_digest = _stream_file_digest(entrypoint_path)
        variant = {
            "platform": platform,
            "architecture": architecture,
            "artifact_digest": digest,
            "entrypoint_digest": entrypoint_digest,
            "relative_path": relative_path,
            "entrypoint": entrypoint,
            "bundle_identity": bundle_identity,
        }
        _validate_binary_architecture(entrypoint_path, architecture)
        verify_platform_artifact(staged_artifacts, variant)

        shell_path = staged_bundle / "shell.tauri.default.shell.v1.json"
        shell = _read_json(shell_path, "Shell")
        matching_targets = [
            target
            for target in shell.get("launch", {}).get("build_targets", [])
            if isinstance(target, Mapping)
            and target.get("platform") == platform
            and target.get("architecture") == architecture
            and _normalize_relative_path(str(target.get("artifact_ref", "")), "Shell artifact ref")
            == relative_path
            and _normalize_relative_path(str(target.get("entrypoint", "")), "Shell entrypoint")
            == entrypoint
            and target.get("bundle_identity") == bundle_identity
        ]
        if len(matching_targets) != 1:
            raise ValueError("packaged artifact does not match one declared Shell build target")
        shell.update(
            shell_api_version="io.tobkiri.shell.v5",
            availability="verified",
            artifact_digest=digest,
        )
        shell["launch"] = {
            "prebuilt_only": True,
            "build_targets": shell["launch"]["build_targets"],
            "variants": [variant],
        }
        shell["provenance"] = _generated_provenance(
            shell,
            "ecosystem/defaultspack/v4/shell.tauri.default.shell.v1.json",
            commit,
            generator_path=Path(__file__),
        )
        shell["definition_revision"] = canonical_digest(
            {key: value for key, value in shell.items() if key != "definition_revision"}
        )
        shell = validate_document(shell, "shell")
        _write_json(shell_path, shell)

        for pack_name in (
            "shell.tauri.default.pack.v4.json",
            "runtime.tauri.application.default.pack.v4.json",
        ):
            pack_path = staged_bundle / "packs" / pack_name
            pack = _read_json(pack_path, f"Pack {pack_name}")
            pack["pack"]["artifact_digest"] = canonical_digest(
                {
                    "pack_id": pack["pack"]["id"],
                    "packaged_artifact_digest": digest,
                }
            )
            retained_artifacts = [
                item
                for item in pack.get("artifacts", ())
                if item.get("kind") != "executable"
            ]
            if pack_name == "runtime.tauri.application.default.pack.v4.json":
                retained_artifacts = [
                    {**item, "platform": "host"} for item in retained_artifacts
                ]
            pack["artifacts"] = [
                {
                    "path": relative_path,
                    "digest": digest,
                    "entrypoint_digest": entrypoint_digest,
                    "kind": "executable",
                    "platform": f"{platform}-{architecture}",
                    "entrypoint": entrypoint,
                    "argv": [],
                },
                *retained_artifacts,
            ]
            pack["pack"]["artifact_digest"] = canonical_digest(pack["artifacts"])
            for function in pack["functions"]:
                function["implementation_digest"] = entrypoint_digest
            pack["provenance"] = _generated_provenance(
                pack,
                f"ecosystem/defaultspack/v4/packs/{pack_name}",
                commit,
                generator_path=Path(__file__),
            )
            _write_json(staged_bundle / "packs" / pack_name, _normalize_pack(pack))

        profile_path = staged_bundle / "defaults.profile.v4.json"
        profile = _read_json(profile_path, "Profile")
        if not isinstance(profile.get("shell"), dict):
            raise ValueError("packaged Profile has no Shell definition")
        profile["shell"].update(
            platform=platform,
            architecture=architecture,
            artifact_digest=digest,
            executable_artifact_digest=entrypoint_digest,
            definition_revision=shell["definition_revision"],
        )
        profile["provenance"] = _generated_provenance(
            profile,
            "ecosystem/defaultspack/v4/defaults.profile.v4.json",
            commit,
            generator_path=Path(__file__),
        )
        _write_json(profile_path, validate_document(profile, "profile"))

        lock_path = staged_bundle / "bundle.lock.json"
        lock = _read_json(lock_path, "bundle lock")
        if not isinstance(lock.get("entries"), list):
            raise ValueError("bundle lock has no entries")
        for entry in lock["entries"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ValueError("bundle lock contains a malformed entry")
            entry["path"] = _normalize_relative_path(entry["path"], "bundle lock path")
            path = _safe_join(staged_bundle, entry["path"], "bundle lock path")
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"bundle lock entry is unavailable: {path}")
            entry["digest"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        _write_json(lock_path, lock)
        _validate_staged_bundle(
            staged_bundle,
            staged_artifacts,
            relative_path,
            entrypoint,
            platform,
            architecture,
            bundle_identity,
            digest,
            entrypoint_digest,
        )
        _publish_directories(
            staged_bundle,
            bundle_root,
            staged_artifacts,
            artifact_root,
        )
    finally:
        if transaction is not None and (transaction.exists() or transaction.is_symlink()):
            _remove_owned(transaction)


def stage_packaged_bundle(
    *,
    source_artifact: Path,
    bundle_root: Path,
    artifact_root: Path,
    relative_path: str,
    entrypoint: str,
    platform: str,
    architecture: str,
    bundle_identity: str,
    source_provenance_file: str | Path | None = None,
) -> None:
    """Snapshot, verify, and atomically publish a packaged Profile bundle."""
    _package_transaction(
        source_artifact=source_artifact,
        bundle_root=bundle_root,
        artifact_root=artifact_root,
        relative_path=relative_path,
        entrypoint=entrypoint,
        platform=platform,
        architecture=architecture,
        bundle_identity=bundle_identity,
        source_provenance_file=source_provenance_file,
    )


def package_bundle(
    *,
    bundle_root: Path,
    artifact_root: Path,
    relative_path: str,
    entrypoint: str,
    platform: str,
    architecture: str,
    bundle_identity: str,
    source_provenance_file: str | Path | None = None,
) -> None:
    """Verify existing staged artifact bytes and atomically rewrite the bundle."""
    _package_transaction(
        source_artifact=None,
        bundle_root=bundle_root,
        artifact_root=artifact_root,
        relative_path=relative_path,
        entrypoint=entrypoint,
        platform=platform,
        architecture=architecture,
        bundle_identity=bundle_identity,
        source_provenance_file=source_provenance_file,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--source-artifact", type=Path)
    parser.add_argument("--relative-path", required=True)
    parser.add_argument("--entrypoint", required=True)
    parser.add_argument("--platform", choices=("macos", "windows", "linux"), required=True)
    parser.add_argument("--architecture", choices=("arm64", "x86_64"), required=True)
    parser.add_argument("--bundle-identity", required=True)
    parser.add_argument("--source-provenance-file", required=True)
    args = parser.parse_args()
    operation = stage_packaged_bundle if args.source_artifact else package_bundle
    operation(
        **({"source_artifact": args.source_artifact} if args.source_artifact else {}),
        bundle_root=args.bundle_root,
        artifact_root=args.artifact_root,
        relative_path=args.relative_path,
        entrypoint=args.entrypoint,
        platform=args.platform,
        architecture=args.architecture,
        bundle_identity=args.bundle_identity,
        source_provenance_file=args.source_provenance_file,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
