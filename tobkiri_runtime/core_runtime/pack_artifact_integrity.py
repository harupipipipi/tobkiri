"""Manifest-only verification for pack runtime and frontend artifacts."""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import secrets
import stat
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterator, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .pack_signature import PackSignatureError, verify_signed_pack
from .paths import ECOSYSTEM_DIR


_SIGNED_MANIFEST_DIGEST_FIELD = "signed_manifest_digest"
_ARTIFACT_DIGEST_FIELD = "artifact_digest"
_INSTALL_PATH_FIELD = "install_path"
_POLICY_GENERATION_FIELD = "policy_generation"
_POLICY_DIGEST_FIELD = "policy_digest"
_MAX_PACK_FILES = 10_000
_MAX_PACK_FILE_BYTES = 128 * 1024 * 1024
_MAX_PACK_TOTAL_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class HostPolicyLock:
    """Descriptor anchor held with one Host policy's process lock."""

    target: Path
    parent_descriptor: int
    parent_identity: tuple[int, int, int, int]


def verify_declared_artifacts(
    pack_root: Path,
    ecosystem_manifest: Mapping[str, Any],
) -> tuple[bool, tuple[str, ...]]:
    """Verify a declared artifact index and every bound file hash."""
    metadata = ecosystem_manifest.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    integrity = metadata.get("integrity")
    integrity = integrity if isinstance(integrity, Mapping) else {}
    signature_diagnostics = _verify_declared_publisher_signature(
        pack_root,
        integrity,
        ecosystem_manifest,
    )
    if signature_diagnostics:
        return False, signature_diagnostics
    relative = str(integrity.get("artifact_manifest") or "").strip()
    if not relative:
        return True, ()
    artifact_path = (pack_root / relative).resolve()
    try:
        artifact_path.relative_to(pack_root.resolve())
    except ValueError:
        return False, ("artifact manifest escapes pack root",)
    try:
        raw = artifact_path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        return False, (f"artifact manifest is unreadable: {type(exc).__name__}",)
    provenance = ecosystem_manifest.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    expected_index_hash = str(provenance.get("content_hash") or "")
    actual_index_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
    diagnostics: list[str] = []
    if actual_index_hash != expected_index_hash:
        diagnostics.append("artifact manifest hash does not match provenance")
    artifacts = payload.get("artifacts") if isinstance(payload, dict) else None
    if not isinstance(artifacts, list):
        diagnostics.append("artifact manifest has no artifacts list")
        return False, tuple(diagnostics)
    for item in artifacts:
        if not isinstance(item, dict):
            diagnostics.append("artifact entry is not an object")
            continue
        path_value = str(item.get("path") or "").strip()
        expected_hash = str(item.get("sha256") or "").strip()
        candidate = (pack_root / path_value).resolve()
        try:
            candidate.relative_to(pack_root.resolve())
        except ValueError:
            diagnostics.append(f"artifact escapes pack root: {path_value}")
            continue
        try:
            actual_hash = "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            diagnostics.append(f"artifact is missing: {path_value}")
            continue
        if actual_hash != expected_hash:
            diagnostics.append(f"artifact hash mismatch: {path_value}")
    return not diagnostics, tuple(diagnostics)


def _verify_declared_publisher_signature(
    pack_root: Path,
    integrity: Mapping[str, Any],
    ecosystem_manifest: Mapping[str, Any],
) -> tuple[str, ...]:
    root = pack_root.resolve()
    trust_store_value = os.environ.get(
        "RUMI_PACK_PUBLISHER_TRUST_STORE",
        "",
    ).strip()
    declared_relative = str(integrity.get("signed_manifest") or "").strip()
    if _is_host_bundled_pack(root, ecosystem_manifest):
        return ()
    if not trust_store_value:
        if declared_relative:
            return ("signed Pack requires a configured publisher trust store",)
        return (
            "non-builtin Pack requires a Host install record and publisher trust store",
        )
    unresolved_trust_store = Path(trust_store_value).expanduser()
    trust_store_path = Path(os.path.abspath(unresolved_trust_store))
    if trust_store_path.is_symlink():
        return ("publisher trust store must not be a symbolic link",)
    try:
        trust_store_path.relative_to(root)
    except ValueError:
        pass
    else:
        return ("publisher trust store must be outside the Pack root",)
    try:
        if trust_store_path.stat().st_mode & 0o022:
            return ("publisher trust store must not be group/world writable",)
        if trust_store_path.parent.stat().st_mode & 0o022:
            return (
                "publisher trust store directory must not be group/world writable",
            )
    except OSError as exc:
        return (f"publisher trust store is unreadable: {type(exc).__name__}",)

    try:
        trust_store = read_host_policy_snapshot(trust_store_path)
        pack_id, pack_version = _pack_identity(ecosystem_manifest, root)
        install_records = trust_store.get("install_records")
        install_records = (
            install_records if isinstance(install_records, Mapping) else {}
        )
        install_record = install_records.get(pack_id)
        install_record = (
            install_record if isinstance(install_record, Mapping) else {}
        )
        if not install_record:
            return ("non-builtin Pack has no Host-owned install record",)
        required_record_fields = {
            "signature_required",
            "publisher_id",
            "key_id",
            "installed_version",
            "signed_manifest_path",
            _SIGNED_MANIFEST_DIGEST_FIELD,
            _ARTIFACT_DIGEST_FIELD,
            _INSTALL_PATH_FIELD,
            "contract_versions",
            "requested_capabilities",
        }
        if install_record and not required_record_fields.issubset(install_record):
            return ("Host install record is incomplete",)
        developer_exception = (
            install_record.get("developer_mode") is True
            and os.environ.get("RUMI_PACK_DEVELOPER_MODE", "").strip().lower()
            in {"1", "true", "yes"}
        )
        signature_required = bool(install_record.get("signature_required"))
        if not signature_required and not developer_exception:
            return ("non-builtin Pack signature is required in normal mode",)
        relative = str(
            install_record.get("signed_manifest_path")
            or declared_relative
            or ""
        ).strip()
        if signature_required and not relative:
            return ("Host install record requires a signed Pack manifest",)
        if not relative:
            if not developer_exception:
                return (
                    "unsigned installed Pack requires explicit Host developer mode",
                )
            try:
                verify_host_install_binding(root, install_record)
            except ValueError as exc:
                return (f"Host install binding verification failed: {exc}",)
            return ()
        if (
            signature_required
            and declared_relative
            and declared_relative != relative
        ):
            return ("Pack signed-manifest declaration differs from Host policy",)
        unresolved_manifest_path = root / relative
        if unresolved_manifest_path.is_symlink():
            return ("signed Pack manifest must not be a symbolic link",)
        manifest_path = unresolved_manifest_path.resolve()
        try:
            manifest_path.relative_to(root)
        except ValueError:
            return ("signed Pack manifest escapes pack root",)
        signed_manifest = _read_json_nofollow(manifest_path, 4 * 1024 * 1024)
        if not isinstance(signed_manifest, Mapping):
            return ("signed Pack manifest is invalid",)
        publisher_id = str(
            install_record.get("publisher_id")
            or signed_manifest.get("publisher_id")
            or ""
        )
        publishers = trust_store.get("publishers")
        publishers = publishers if isinstance(publishers, Mapping) else {}
        publisher = publishers.get(publisher_id)
        publisher = publisher if isinstance(publisher, Mapping) else {}
        namespaces = [
            str(item)
            for item in publisher.get("allowed_pack_namespaces") or []
            if str(item)
        ]
        if namespaces and not any(
            pack_id == namespace or pack_id.startswith(f"{namespace}.")
            for namespace in namespaces
        ):
            return ("publisher is not allowed to sign this Pack namespace",)
        public_key = serialization.load_pem_public_key(
            str(publisher.get("public_key_pem") or "").encode("utf-8")
        )
        if not isinstance(public_key, Ed25519PublicKey):
            return ("publisher trust key is not Ed25519",)
        revoked = {
            str(item)
            for item in publisher.get("revoked_key_ids") or []
            if str(item)
        }
        verify_signed_pack(
            root,
            dict(signed_manifest),
            public_key,
            expected_publisher_id=publisher_id,
            expected_pack_id=pack_id,
            expected_version=(
                str(install_record.get("installed_version") or pack_version)
                if install_record or pack_version
                else None
            ),
            expected_key_id=str(install_record.get("key_id") or "") or None,
            expected_contract_versions=(
                dict(install_record.get("contract_versions") or {})
                if "contract_versions" in install_record
                else None
            ),
            expected_capabilities=(
                [
                    str(item)
                    for item in install_record.get("requested_capabilities") or []
                ]
                if "requested_capabilities" in install_record
                else None
            ),
            revoked_key_ids=revoked,
            core_version=_core_version(),
        )
        verify_host_install_binding(root, install_record, signed_manifest)
    except (
        OSError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        PackSignatureError,
    ) as exc:
        return (f"signed Pack verification failed: {exc}",)
    return ()


def _is_host_bundled_pack(
    pack_root: Path,
    ecosystem_manifest: Mapping[str, Any],
) -> bool:
    """Recognize only Packs physically shipped in the immutable bundle root."""

    try:
        root = pack_root.resolve(strict=True)
        bundled_root = Path(ECOSYSTEM_DIR).resolve(strict=True)
        root.relative_to(bundled_root)
    except (OSError, ValueError):
        return False
    pack_id, _ = _pack_identity(ecosystem_manifest, root)
    return root.parent == bundled_root and root.name == pack_id


def _read_json_nofollow(
    path: Path,
    max_bytes: int,
    *,
    require_private: bool = False,
) -> Any:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("JSON policy path is not a regular file")
        if require_private and stat.S_IMODE(before.st_mode) & 0o022:
            raise ValueError("JSON policy file is writable by other users")
        if before.st_size > max_bytes:
            raise ValueError("JSON policy file exceeds size limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("JSON policy file exceeds size limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("JSON policy file changed while reading")
        return json.loads(b"".join(chunks).decode("utf-8"))
    finally:
        os.close(descriptor)


def write_host_install_record(
    trust_store_path: Path,
    *,
    pack_id: str,
    install_path: Path,
    record: Mapping[str, Any],
) -> None:
    """Atomically persist a complete Host-owned Pack install policy.

    ``install_path`` is a Host-selected path, not a Pack-supplied manifest
    field.  The writer derives the canonical path and exact content digests so
    callers cannot accidentally authorize a Pack identity without authorizing
    the concrete artifact that was reviewed.
    """

    required = {
        "signature_required",
        "publisher_id",
        "key_id",
        "installed_version",
        "signed_manifest_path",
        "contract_versions",
        "requested_capabilities",
    }
    if not set(record).issubset(required | {"developer_mode"}) or not required.issubset(
        record
    ):
        raise ValueError("Host install record fields are incomplete or unknown")
    developer_exception = record.get("developer_mode") is True
    if record.get("signature_required") is not True and not developer_exception:
        raise ValueError("installed publisher Pack signatures must be required")
    identity_fields = (
        ("installed_version",)
        if developer_exception
        else (
            "publisher_id",
            "key_id",
            "installed_version",
            "signed_manifest_path",
        )
    )
    if any(not str(record.get(field) or "").strip() for field in identity_fields):
        raise ValueError("Host install record identity fields are required")
    if developer_exception and str(record.get("signed_manifest_path") or "").strip():
        raise ValueError(
            "developer-mode unsigned install record must not declare a signed manifest"
        )
    root = _canonical_install_root(install_path)
    root_identity = _directory_identity(root)
    signed_manifest_relative = str(record.get("signed_manifest_path") or "").strip()
    signed_manifest_digest = ""
    if signed_manifest_relative:
        signed_manifest_path = _contained_regular_file(
            root,
            signed_manifest_relative,
            "signed Pack manifest",
        )
        signed_manifest = _read_json_nofollow(
            signed_manifest_path,
            4 * 1024 * 1024,
        )
        if not isinstance(signed_manifest, Mapping):
            raise ValueError("signed Pack manifest must be an object")
        signed_manifest_digest = _canonical_digest(signed_manifest)
    tree_digest = _pack_tree_digest(
        root,
        signed_manifest_exclusion=(
            signed_manifest_relative if signed_manifest_relative else None
        ),
    )
    artifact_digest = _install_artifact_digest(
        tree_digest,
        signed_manifest_digest,
    )
    if _directory_identity(root) != root_identity:
        raise ValueError("install path changed while capturing Host policy")
    persisted_record = {
        **dict(record),
        _INSTALL_PATH_FIELD: str(root),
        _SIGNED_MANIFEST_DIGEST_FIELD: signed_manifest_digest,
        _ARTIFACT_DIGEST_FIELD: artifact_digest,
    }
    target = _canonical_policy_target(trust_store_path, require_parent=False)
    if target.is_symlink():
        raise ValueError("publisher trust store must not be a symbolic link")
    with exclusive_host_policy_lock(target) as policy_lock:
        payload, file_identity = _read_host_policy_state(
            target,
            allow_missing=True,
            policy_lock=policy_lock,
        )
        previous_policy_identity = host_policy_identity(payload)
        records = payload.get("install_records")
        records = dict(records) if isinstance(records, Mapping) else {}
        records[str(pack_id)] = persisted_record
        payload["install_records"] = records
        generation = _policy_generation(payload) + 1
        payload[_POLICY_GENERATION_FIELD] = generation
        payload[_POLICY_DIGEST_FIELD] = _policy_digest(payload)
        _write_private_policy(
            target,
            payload,
            expected_file_identity=file_identity,
            expected_policy_identity=previous_policy_identity,
            policy_lock=policy_lock,
        )


def verify_host_install_binding(
    pack_root: Path,
    install_record: Mapping[str, Any],
    signed_manifest: Mapping[str, Any] | None = None,
    *,
    authorized_install_path: Path | None = None,
) -> None:
    """Verify one Pack tree against its exact Host-owned install binding.

    ``authorized_install_path`` is used only while the Host copies an already
    verified source into its private CAS.  Runtime callers must omit it so the
    executable path itself must equal the recorded canonical install path.
    """

    root = _canonical_install_root(pack_root)
    authorized_root = _canonical_install_root(authorized_install_path or root)
    recorded_path = str(install_record.get(_INSTALL_PATH_FIELD) or "").strip()
    if not recorded_path or recorded_path != str(authorized_root):
        raise ValueError("Pack path differs from Host install record")
    expected_manifest_digest = str(
        install_record.get(_SIGNED_MANIFEST_DIGEST_FIELD) or ""
    ).strip()
    if signed_manifest is None:
        if expected_manifest_digest:
            raise ValueError("Host install record requires a signed manifest")
    else:
        actual_manifest_digest = _canonical_digest(signed_manifest)
        if not expected_manifest_digest or not _digest_equal(
            expected_manifest_digest,
            actual_manifest_digest,
        ):
            raise ValueError("signed manifest differs from Host install record")
    expected_artifact_digest = str(
        install_record.get(_ARTIFACT_DIGEST_FIELD) or ""
    ).strip()
    tree_digest = _pack_tree_digest(
        root,
        signed_manifest_exclusion=(
            str(install_record.get("signed_manifest_path") or "").strip()
            if signed_manifest is not None
            else None
        ),
    )
    actual_artifact_digest = _install_artifact_digest(
        tree_digest,
        actual_manifest_digest if signed_manifest is not None else "",
    )
    if not expected_artifact_digest or not _digest_equal(
        expected_artifact_digest,
        actual_artifact_digest,
    ):
        raise ValueError("Pack artifact differs from Host install record")


def _canonical_install_root(path: Path) -> Path:
    unresolved = Path(path).expanduser()
    if unresolved.is_symlink():
        raise ValueError("install path must not be a symbolic link")
    try:
        root = unresolved.resolve(strict=True)
        metadata = root.lstat()
    except OSError as exc:
        raise ValueError("install path is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("install path must be a directory")
    return root


def _contained_regular_file(root: Path, relative: str, label: str) -> Path:
    pure = PurePosixPath(relative)
    if (
        not relative
        or pure.is_absolute()
        or ".." in pure.parts
        or "." in pure.parts
        or "\\" in relative
    ):
        raise ValueError(f"{label} path is unsafe")
    candidate = root.joinpath(*pure.parts)
    if candidate.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        metadata = resolved.lstat()
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a regular file")
    return resolved


def _pack_tree_digest(
    root: Path,
    *,
    signed_manifest_exclusion: str | None,
) -> str:
    """Hash a finite Pack tree from one descriptor-anchored root."""

    if os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd:
        raise ValueError("secure descriptor-relative Pack capture is unavailable")
    initial = root.lstat()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root, flags)
    try:
        opened = os.fstat(descriptor)
        if _file_identity(initial) != _file_identity(opened):
            raise ValueError("Pack root changed before hashing")
        records: list[dict[str, Any]] = []
        counters = [0, 0]
        _capture_directory_records(
            descriptor,
            (),
            records,
            counters,
            signed_manifest_exclusion=signed_manifest_exclusion,
        )
        if _file_identity(opened) != _file_identity(os.fstat(descriptor)):
            raise ValueError("Pack root changed while hashing")
    finally:
        os.close(descriptor)
    return _canonical_digest(sorted(records, key=lambda item: item["path"]))


def _capture_directory_records(
    directory_descriptor: int,
    parent_parts: tuple[str, ...],
    records: list[dict[str, Any]],
    counters: list[int],
    *,
    signed_manifest_exclusion: str | None,
) -> None:
    names = os.listdir(directory_descriptor)
    collision_keys: set[str] = set()
    for name in names:
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise ValueError("Pack tree contains an unsafe name")
        normalized = unicodedata.normalize("NFC", name)
        collision_key = normalized.casefold()
        if name != normalized or collision_key in collision_keys:
            raise ValueError("Pack tree contains a normalized path collision")
        collision_keys.add(collision_key)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    for name in sorted(names):
        metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        parts = (*parent_parts, name)
        relative = "/".join(parts)
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(name, directory_flags, dir_fd=directory_descriptor)
            try:
                if _file_identity(metadata) != _file_identity(os.fstat(child)):
                    raise ValueError("Pack directory changed during capture")
                _capture_directory_records(
                    child,
                    parts,
                    records,
                    counters,
                    signed_manifest_exclusion=signed_manifest_exclusion,
                )
                if _file_identity(metadata) != _file_identity(os.fstat(child)):
                    raise ValueError("Pack directory changed during capture")
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Pack tree contains a non-regular entry")
        if signed_manifest_exclusion is not None and relative == signed_manifest_exclusion:
            continue
        content, mode = _read_regular_at(directory_descriptor, name, metadata)
        counters[0] += 1
        counters[1] += len(content)
        if counters[0] > _MAX_PACK_FILES:
            raise ValueError("Pack exceeds the file-count limit")
        if counters[1] > _MAX_PACK_TOTAL_BYTES:
            raise ValueError("Pack exceeds the total-size limit")
        records.append(
            {
                "path": relative,
                "size": len(content),
                "mode": stat.S_IMODE(mode),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )


def _read_regular_at(
    directory_descriptor: int,
    name: str,
    expected: os.stat_result,
) -> tuple[bytes, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _file_identity(expected) != _file_identity(before)
        ):
            raise ValueError("Pack artifact identity is unsafe")
        if before.st_size > _MAX_PACK_FILE_BYTES:
            raise ValueError("Pack artifact exceeds the file-size limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, _MAX_PACK_FILE_BYTES + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_PACK_FILE_BYTES:
                raise ValueError("Pack artifact exceeds the file-size limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(after):
            raise ValueError("Pack artifact changed while reading")
        return b"".join(chunks), before.st_mode
    finally:
        os.close(descriptor)


def _directory_identity(path: Path) -> tuple[int, int, int]:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("install path must be a real directory")
    return int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_mtime_ns)


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _install_artifact_digest(tree_digest: str, signed_manifest_digest: str) -> str:
    if not signed_manifest_digest:
        return tree_digest
    return _canonical_digest(
        {
            "signed_manifest_digest": signed_manifest_digest,
            "tree_digest": tree_digest,
        }
    )


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _digest_equal(left: str, right: str) -> bool:
    return len(left) == len(right) and hmac.compare_digest(left, right)


def read_host_policy_snapshot(
    path: Path,
    *,
    allow_missing: bool = False,
    policy_lock: HostPolicyLock | None = None,
) -> dict[str, Any]:
    """Read one Host policy through a verified descriptor chain."""

    value, _identity = _read_host_policy_state(
        path,
        allow_missing=allow_missing,
        policy_lock=policy_lock,
    )
    return value


def _read_host_policy_state(
    path: Path,
    *,
    allow_missing: bool,
    policy_lock: HostPolicyLock | None = None,
) -> tuple[dict[str, Any], tuple[int, int, int, int, int] | None]:
    target = _canonical_policy_target(path, require_parent=True)
    parent_descriptor = _policy_parent_descriptor(target, policy_lock)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target.name, flags, dir_fd=parent_descriptor)
        except FileNotFoundError:
            if allow_missing:
                return {}, None
            raise
        try:
            metadata = os.fstat(descriptor)
            _require_private_regular(metadata, "publisher trust store")
            content = _read_bounded_descriptor(descriptor, 4 * 1024 * 1024)
            identity = _policy_file_identity(metadata)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("publisher trust store is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("publisher trust store must be an object")
    _validate_policy_envelope(value)
    return value, identity


def host_policy_identity(policy: Mapping[str, Any]) -> tuple[int, str]:
    """Return the generation and digest of a validated Host policy snapshot."""

    _validate_policy_envelope(policy)
    generation = _policy_generation(policy)
    digest = str(policy.get(_POLICY_DIGEST_FIELD) or "")
    return generation, digest


@contextmanager
def exclusive_host_policy_lock(path: Path) -> Iterator[HostPolicyLock]:
    """Hold the cross-process lock associated with one Host policy file."""

    target = _canonical_policy_target(path, require_parent=False)
    created_parent_descriptor = _open_verified_directory_chain(
        target.parent,
        create=True,
    )
    os.close(created_parent_descriptor)
    target = _canonical_policy_target(target, require_parent=True)
    parent_descriptor = _open_verified_directory_chain(target.parent)
    lock_descriptor: int | None = None
    backend = ""
    try:
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        for attempt in range(2):
            try:
                lock_descriptor = os.open(
                    f".{target.name}.lock",
                    flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
                break
            except FileNotFoundError:
                if attempt:
                    raise
        if lock_descriptor is None:
            raise ValueError("Host policy lock is unavailable")
        metadata = os.fstat(lock_descriptor)
        _require_private_regular(metadata, "Host policy lock")
        if os.name == "nt":
            locking = importlib.import_module("msvcrt")
            if metadata.st_size == 0:
                os.write(lock_descriptor, b"0")
                os.fsync(lock_descriptor)
            os.lseek(lock_descriptor, 0, os.SEEK_SET)
            getattr(locking, "locking")(
                lock_descriptor,
                getattr(locking, "LK_LOCK"),
                1,
            )
            backend = "msvcrt"
        else:
            locking = importlib.import_module("fcntl")
            getattr(locking, "flock")(
                lock_descriptor,
                getattr(locking, "LOCK_EX"),
            )
            backend = "fcntl"
        yield HostPolicyLock(
            target=target,
            parent_descriptor=parent_descriptor,
            parent_identity=_file_identity(os.fstat(parent_descriptor)),
        )
    finally:
        if lock_descriptor is not None:
            try:
                if backend == "msvcrt":
                    os.lseek(lock_descriptor, 0, os.SEEK_SET)
                    getattr(locking, "locking")(
                        lock_descriptor,
                        getattr(locking, "LK_UNLCK"),
                        1,
                    )
                elif backend == "fcntl":
                    getattr(locking, "flock")(
                        lock_descriptor,
                        getattr(locking, "LOCK_UN"),
                    )
            finally:
                os.close(lock_descriptor)
        os.close(parent_descriptor)


def _open_verified_directory_chain(path: Path, *, create: bool = False) -> int:
    absolute = Path(os.path.abspath(path))
    if os.name == "nt":
        raise ValueError("secure Host policy directory traversal is unavailable")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(absolute.anchor or "/", flags)
    try:
        _require_safe_directory(os.fstat(descriptor), absolute.anchor or "/")
        for component in absolute.parts[1:]:
            try:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            _require_safe_directory(os.fstat(descriptor), component)
        final = os.fstat(descriptor)
        effective_uid = getattr(os, "geteuid", lambda: final.st_uid)()
        if final.st_uid not in {0, effective_uid}:
            raise ValueError("Host policy directory owner is unsafe")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _require_safe_directory(metadata: os.stat_result, label: str) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"Host policy ancestor is not a directory: {label}")
    effective_uid = getattr(os, "geteuid", lambda: metadata.st_uid)()
    if metadata.st_uid not in {0, effective_uid}:
        raise ValueError(f"Host policy ancestor owner is unsafe: {label}")
    writable = stat.S_IMODE(metadata.st_mode) & 0o022
    sticky = stat.S_IMODE(metadata.st_mode) & stat.S_ISVTX
    if writable and not (metadata.st_uid == 0 and sticky):
        raise ValueError(f"Host policy ancestor permissions are unsafe: {label}")


def _require_private_regular(metadata: os.stat_result, label: str) -> None:
    effective_uid = getattr(os, "geteuid", lambda: metadata.st_uid)()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != effective_uid
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError(f"{label} identity or permissions are unsafe")


def _read_bounded_descriptor(descriptor: int, max_bytes: int) -> bytes:
    before = os.fstat(descriptor)
    if before.st_size > max_bytes:
        raise ValueError("Host policy exceeds the size limit")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("Host policy exceeds the size limit")
        chunks.append(chunk)
    if _file_identity(before) != _file_identity(os.fstat(descriptor)):
        raise ValueError("Host policy changed while reading")
    return b"".join(chunks)


def _write_private_policy(
    path: Path,
    policy: Mapping[str, Any],
    *,
    expected_file_identity: tuple[int, int, int, int, int] | None,
    expected_policy_identity: tuple[int, str],
    policy_lock: HostPolicyLock | None = None,
) -> None:
    target = _canonical_policy_target(path, require_parent=True)
    parent_descriptor = _policy_parent_descriptor(target, policy_lock)
    temporary_name = f".{target.name}.{os.getpid()}.{secrets.token_hex(16)}"
    descriptor: int | None = None
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        encoded = (
            json.dumps(
                dict(policy),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        current_policy, current_identity = _read_policy_from_parent_descriptor(
            parent_descriptor,
            target.name,
            allow_missing=expected_file_identity is None,
        )
        if (
            current_identity != expected_file_identity
            or host_policy_identity(current_policy) != expected_policy_identity
        ):
            raise ValueError("Host policy changed during compare-and-swap")
        os.replace(
            temporary_name,
            target.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        final_descriptor = os.open(
            target.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        try:
            _require_private_regular(
                os.fstat(final_descriptor),
                "publisher trust store",
            )
            os.fsync(final_descriptor)
        finally:
            os.close(final_descriptor)
        os.fsync(parent_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        os.close(parent_descriptor)


def _read_policy_from_parent_descriptor(
    parent_descriptor: int,
    name: str,
    *,
    allow_missing: bool,
) -> tuple[dict[str, Any], tuple[int, int, int, int, int] | None]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError:
        if allow_missing:
            return {}, None
        raise ValueError("Host policy disappeared during compare-and-swap") from None
    try:
        metadata = os.fstat(descriptor)
        _require_private_regular(metadata, "publisher trust store")
        content = _read_bounded_descriptor(descriptor, 4 * 1024 * 1024)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("publisher trust store is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("publisher trust store must be an object")
    _validate_policy_envelope(value)
    return value, _policy_file_identity(metadata)


def _canonical_policy_target(path: Path, *, require_parent: bool) -> Path:
    unresolved = Path(path).expanduser()
    target = Path(os.path.abspath(unresolved))
    if require_parent:
        try:
            metadata = target.parent.lstat()
        except OSError as exc:
            raise ValueError("Host policy directory is unavailable") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("Host policy directory is unavailable")
    return target


def _policy_parent_descriptor(
    target: Path,
    policy_lock: HostPolicyLock | None,
) -> int:
    if policy_lock is None:
        return _open_verified_directory_chain(target.parent)
    if target != policy_lock.target:
        raise ValueError("Host policy lock target does not match")
    try:
        current_parent = target.parent.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError("Host policy directory changed while locked") from exc
    if _file_identity(current_parent) != policy_lock.parent_identity:
        raise ValueError("Host policy directory changed while locked")
    descriptor = os.dup(policy_lock.parent_descriptor)
    if _file_identity(os.fstat(descriptor)) != policy_lock.parent_identity:
        os.close(descriptor)
        raise ValueError("Host policy directory changed while locked")
    return descriptor


def _policy_file_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _policy_generation(policy: Mapping[str, Any]) -> int:
    value = policy.get(_POLICY_GENERATION_FIELD, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Host policy generation is invalid")
    return value


def _policy_digest(policy: Mapping[str, Any]) -> str:
    return _canonical_digest(
        {key: value for key, value in policy.items() if key != _POLICY_DIGEST_FIELD}
    )


def _validate_policy_envelope(policy: Mapping[str, Any]) -> None:
    generation_present = _POLICY_GENERATION_FIELD in policy
    digest_present = _POLICY_DIGEST_FIELD in policy
    if generation_present != digest_present:
        raise ValueError("Host policy generation envelope is incomplete")
    if not generation_present:
        return
    _policy_generation(policy)
    supplied = str(policy.get(_POLICY_DIGEST_FIELD) or "")
    expected = _policy_digest(policy)
    if not supplied or not _digest_equal(supplied, expected):
        raise ValueError("Host policy digest is invalid")


def _core_version() -> str:
    from rumi_ai import __version__

    return str(__version__)


def _pack_identity(
    ecosystem_manifest: Mapping[str, Any],
    root: Path,
) -> tuple[str, str]:
    pack = ecosystem_manifest.get("pack")
    pack = pack if isinstance(pack, Mapping) else {}
    pack_id = str(
        pack.get("id")
        or ecosystem_manifest.get("id")
        or ecosystem_manifest.get("pack_id")
        or root.name
    ).strip()
    version = str(
        pack.get("version")
        or ecosystem_manifest.get("version")
        or ""
    ).strip()
    return pack_id, version
