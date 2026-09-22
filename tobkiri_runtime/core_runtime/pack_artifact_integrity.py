"""Manifest-only verification for pack runtime and frontend artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .pack_signature import PackSignatureError, verify_signed_pack
from .paths import ECOSYSTEM_DIR


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
    if unresolved_trust_store.is_symlink():
        return ("publisher trust store must not be a symbolic link",)
    trust_store_path = unresolved_trust_store.resolve()
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
        trust_store = _read_json_nofollow(trust_store_path, 4 * 1024 * 1024)
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
            signed_manifest,
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


def _read_json_nofollow(path: Path, max_bytes: int) -> Any:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
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
    record: Mapping[str, Any],
) -> None:
    """Atomically persist a complete Host-owned Pack install policy."""

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
    unresolved_target = trust_store_path.expanduser()
    if unresolved_target.is_symlink():
        raise ValueError("publisher trust store must not be a symbolic link")
    target = unresolved_target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    payload: dict[str, Any] = {}
    if target.exists():
        if target.stat().st_mode & 0o022:
            raise ValueError("publisher trust store is writable by other users")
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("publisher trust store must be an object")
    records = payload.get("install_records")
    records = dict(records) if isinstance(records, Mapping) else {}
    records[str(pack_id)] = dict(record)
    payload["install_records"] = records
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=target.parent,
    )
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


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
