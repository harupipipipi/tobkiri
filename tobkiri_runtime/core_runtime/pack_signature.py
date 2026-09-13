"""Signed Pack integrity envelopes.

Signatures prove artifact integrity and publisher identity. They deliberately
do not grant capabilities, approval, trust, or host authority.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

SIGNED_PACK_VERSION = "tobkiri.signed-pack/v1"
SIGNED_MANIFEST_RELATIVE = ".tobkiri/signed-pack.json"
MAX_SIGNED_FILES = 10_000
MAX_SIGNED_FILE_BYTES = 128 * 1024 * 1024
MAX_SIGNED_TOTAL_BYTES = 512 * 1024 * 1024
_SENSITIVE_NAMES = {
    ".env",
    "credentials",
    "credentials.json",
    "secrets.json",
}
_SENSITIVE_SUFFIXES = {
    ".db",
    ".key",
    ".log",
    ".pem",
    ".sqlite",
    ".sqlite3",
}
_RUNTIME_PARTS = {
    ".cache",
    ".pytest_cache",
    "__pycache__",
    "cache",
    "logs",
    "node_modules",
    "tmp",
}


class PackSignatureError(ValueError):
    """Raised when a Pack signature or signed artifact is invalid."""


def build_signed_manifest(
    pack_root: Path,
    *,
    pack_id: str,
    version: str,
    publisher_id: str,
    core_compatibility: str,
    contract_versions: dict[str, str] | None = None,
    requested_capabilities: list[str] | None = None,
    build_provenance: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build an unsigned, deterministic file manifest for a Pack directory."""

    root = pack_root.resolve()
    if not root.is_dir():
        raise PackSignatureError("pack_root must be an existing directory")
    normalized_pack_id = _required_text(pack_id, "pack_id")
    normalized_version = _required_text(version, "version")
    normalized_publisher = _required_text(publisher_id, "publisher_id")
    files = _file_records(root)
    if not files:
        raise PackSignatureError("Pack has no signable files")
    return {
        "api_version": SIGNED_PACK_VERSION,
        "pack_id": normalized_pack_id,
        "version": normalized_version,
        "publisher_id": normalized_publisher,
        "core_compatibility": _required_text(
            core_compatibility,
            "core_compatibility",
        ),
        "files": files,
        "contract_versions": dict(sorted((contract_versions or {}).items())),
        "requested_capabilities": sorted(
            {
                _required_text(item, "requested_capability")
                for item in requested_capabilities or []
            }
        ),
        "build_provenance": build_provenance or {},
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "authority_granted": False,
    }


def sign_manifest(
    manifest: dict[str, Any],
    private_key: Ed25519PrivateKey,
) -> dict[str, Any]:
    """Attach an Ed25519 signature without changing Pack authority."""

    unsigned = _unsigned_manifest(manifest)
    _validate_unsigned_manifest(unsigned)
    public_key = private_key.public_key()
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = hashlib.sha256(public_bytes).hexdigest()[:32]
    signature = private_key.sign(_canonical_bytes(unsigned))
    return {
        **unsigned,
        "signature": {
            "algorithm": "Ed25519",
            "key_id": key_id,
            "value": _b64url(signature),
        },
    }


def verify_signed_pack(
    pack_root: Path,
    manifest: dict[str, Any],
    public_key: Ed25519PublicKey,
    *,
    expected_publisher_id: str | None = None,
    expected_pack_id: str | None = None,
    expected_version: str | None = None,
    expected_key_id: str | None = None,
    expected_contract_versions: dict[str, str] | None = None,
    expected_capabilities: list[str] | None = None,
    revoked_key_ids: set[str] | None = None,
    core_version: str | None = None,
) -> dict[str, Any]:
    """Verify signature, publisher, revocation, and the complete file set."""

    unsigned = _unsigned_manifest(manifest)
    raw_signature = manifest.get("signature")
    signature: dict[str, Any] = (
        raw_signature if isinstance(raw_signature, dict) else {}
    )
    if signature.get("algorithm") != "Ed25519":
        raise PackSignatureError("unsupported or missing signature algorithm")
    if set(signature) != {"algorithm", "key_id", "value"}:
        raise PackSignatureError("signature envelope contains unknown fields")
    _validate_unsigned_manifest(unsigned)
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_key_id = hashlib.sha256(public_bytes).hexdigest()[:32]
    if signature.get("key_id") != public_key_id:
        raise PackSignatureError("signature key id mismatch")
    if expected_key_id is not None and public_key_id != expected_key_id:
        raise PackSignatureError("Host install record key id mismatch")
    if public_key_id in (revoked_key_ids or set()):
        raise PackSignatureError("publisher signing key is revoked")
    if (
        expected_publisher_id is not None
        and unsigned.get("publisher_id") != expected_publisher_id
    ):
        raise PackSignatureError("publisher identity mismatch")
    if expected_pack_id is not None and unsigned.get("pack_id") != expected_pack_id:
        raise PackSignatureError("Pack identity mismatch")
    if expected_version is not None and unsigned.get("version") != expected_version:
        raise PackSignatureError("Pack version mismatch")
    if (
        expected_contract_versions is not None
        and unsigned.get("contract_versions")
        != dict(sorted(expected_contract_versions.items()))
    ):
        raise PackSignatureError("Pack contract versions mismatch")
    if (
        expected_capabilities is not None
        and unsigned.get("requested_capabilities")
        != sorted(set(expected_capabilities))
    ):
        raise PackSignatureError("Pack requested capabilities mismatch")
    if core_version is not None:
        try:
            compatible = Version(core_version) in SpecifierSet(
                str(unsigned["core_compatibility"])
            )
        except (InvalidSpecifier, InvalidVersion) as exc:
            raise PackSignatureError("core compatibility is invalid") from exc
        if not compatible:
            raise PackSignatureError(
                f"Pack is incompatible with core version {core_version}"
            )
    try:
        public_key.verify(
            _unb64url(str(signature.get("value") or "")),
            _canonical_bytes(unsigned),
        )
    except (InvalidSignature, ValueError) as exc:
        raise PackSignatureError("Pack signature mismatch") from exc

    current_files = _file_records(pack_root.resolve())
    if current_files != unsigned.get("files"):
        raise PackSignatureError("Pack file manifest mismatch")
    return {
        "verified": True,
        "pack_id": unsigned.get("pack_id"),
        "version": unsigned.get("version"),
        "publisher_id": unsigned.get("publisher_id"),
        "key_id": public_key_id,
        "authority_granted": False,
    }


def _file_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    total_bytes = 0
    normalized_paths: set[str] = set()
    root_mode = stat.S_IMODE(root.lstat().st_mode)
    if root_mode & 0o022:
        raise PackSignatureError(
            "Pack root must not be group/world writable"
        )
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        current_mode = stat.S_IMODE(current_path.lstat().st_mode)
        if current_mode & 0o022:
            raise PackSignatureError(
                f"Pack directory must not be group/world writable: "
                f"{current_path.relative_to(root).as_posix() or '.'}"
            )
        for directory in sorted(directories):
            path = current_path / directory
            relative = path.relative_to(root)
            if path.is_symlink():
                raise PackSignatureError(
                    f"Pack contains a symbolic link: {relative.as_posix()}"
                )
            if directory.casefold() in _RUNTIME_PARTS:
                raise PackSignatureError(
                    f"Pack contains a runtime directory: {relative.as_posix()}"
                )
            if directory.casefold() == ".git":
                raise PackSignatureError(
                    f"Pack contains repository metadata: {relative.as_posix()}"
                )
        directories[:] = sorted(
            directory
            for directory in directories
            if directory.casefold() not in _RUNTIME_PARTS
        )
        for filename in sorted(filenames):
            path = current_path / filename
            relative = path.relative_to(root)
            if path.is_symlink():
                raise PackSignatureError(
                    f"Pack contains a symbolic link: {relative.as_posix()}"
                )
            if relative.as_posix() == SIGNED_MANIFEST_RELATIVE:
                continue
            _validate_package_path(relative)
            collision_key = unicodedata.normalize(
                "NFC",
                relative.as_posix(),
            ).casefold()
            if collision_key in normalized_paths:
                raise PackSignatureError(
                    f"Pack contains a normalized path collision: {relative.as_posix()}"
                )
            normalized_paths.add(collision_key)
            digest, size, mode = _hash_regular_file(path)
            total_bytes += size
            if len(records) >= MAX_SIGNED_FILES:
                raise PackSignatureError("Pack exceeds the signed file-count limit")
            if total_bytes > MAX_SIGNED_TOTAL_BYTES:
                raise PackSignatureError("Pack exceeds the signed total-size limit")
            records.append(
                {
                    "path": relative.as_posix(),
                    "size": size,
                    "mode": mode,
                    "sha256": digest,
                }
            )
    return sorted(records, key=lambda record: str(record["path"]))


def _unsigned_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in manifest.items()
        if key != "signature"
    }


def _validate_unsigned_manifest(manifest: dict[str, Any]) -> None:
    required = {
        "api_version",
        "pack_id",
        "version",
        "publisher_id",
        "core_compatibility",
        "files",
        "contract_versions",
        "requested_capabilities",
        "build_provenance",
        "created_at",
        "authority_granted",
    }
    if set(manifest) != required:
        raise PackSignatureError("signed Pack manifest fields are invalid")
    if manifest.get("api_version") != SIGNED_PACK_VERSION:
        raise PackSignatureError("unsupported signed Pack manifest version")
    for field in (
        "pack_id",
        "version",
        "publisher_id",
        "core_compatibility",
        "created_at",
    ):
        _required_text(manifest.get(field), field)
    try:
        SpecifierSet(str(manifest["core_compatibility"]))
    except InvalidSpecifier as exc:
        raise PackSignatureError("core_compatibility is invalid") from exc
    try:
        created_at = datetime.fromisoformat(
            str(manifest["created_at"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise PackSignatureError("created_at must be an ISO timestamp") from exc
    if created_at.tzinfo is None:
        raise PackSignatureError("created_at must include a timezone")
    if created_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise PackSignatureError("created_at may not be in the future")
    if manifest.get("authority_granted") is not False:
        raise PackSignatureError("signed Pack manifests may not grant authority")
    if not isinstance(manifest.get("build_provenance"), dict):
        raise PackSignatureError("build_provenance must be an object")
    contracts = manifest.get("contract_versions")
    if not isinstance(contracts, dict) or any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or not key.strip()
        or not value.strip()
        for key, value in contracts.items()
    ):
        raise PackSignatureError("contract_versions must be a string map")
    capabilities = manifest.get("requested_capabilities")
    if (
        not isinstance(capabilities, list)
        or any(not isinstance(item, str) or not item.strip() for item in capabilities)
    ):
        raise PackSignatureError("requested_capabilities must be unique strings")
    if len(capabilities) != len(set(capabilities)):
        raise PackSignatureError("requested_capabilities must be unique strings")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise PackSignatureError("files must be a non-empty array")
    seen: set[str] = set()
    for record in files:
        if not isinstance(record, dict) or set(record) != {
            "path",
            "size",
            "mode",
            "sha256",
        }:
            raise PackSignatureError("signed file record is invalid")
        path = str(record.get("path") or "")
        pure_path = PurePosixPath(path)
        if (
            not path
            or pure_path.is_absolute()
            or ".." in pure_path.parts
            or path in seen
        ):
            raise PackSignatureError("signed file path is invalid")
        seen.add(path)
        size = record.get("size")
        mode = record.get("mode")
        digest = str(record.get("sha256") or "")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise PackSignatureError("signed file size is invalid")
        if (
            isinstance(mode, bool)
            or not isinstance(mode, int)
            or mode not in {0o644, 0o755}
        ):
            raise PackSignatureError("signed file mode is invalid")
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise PackSignatureError("signed file digest is invalid")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _required_text(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 256:
        raise PackSignatureError(f"{field} must be 1-256 characters")
    return normalized


def _validate_package_path(relative: Path) -> None:
    parts = relative.parts
    lowered = [part.casefold() for part in parts]
    filename = lowered[-1]
    for original, lowered_part in zip(parts, lowered, strict=True):
        if original.endswith((" ", ".")) or ":" in original:
            raise PackSignatureError(
                f"Pack contains an unsafe Windows path: {relative.as_posix()}"
            )
        stem = lowered_part.split(".", 1)[0]
        if stem in {"con", "prn", "aux", "nul"} or re.fullmatch(
            r"(com|lpt)[1-9]",
            stem,
        ):
            raise PackSignatureError(
                f"Pack contains a Windows-reserved path: {relative.as_posix()}"
            )
    if (
        filename in _SENSITIVE_NAMES
        or Path(filename).suffix in _SENSITIVE_SUFFIXES
        or any(part in _RUNTIME_PARTS for part in lowered[:-1])
    ):
        raise PackSignatureError(
            f"Pack contains a secret or runtime file: {relative.as_posix()}"
        )
def _hash_regular_file(path: Path) -> tuple[str, int, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PackSignatureError(f"Pack entry is not a regular file: {path.name}")
        if before.st_size > MAX_SIGNED_FILE_BYTES:
            raise PackSignatureError(f"Pack file exceeds size limit: {path.name}")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SIGNED_FILE_BYTES:
                raise PackSignatureError(f"Pack file exceeds size limit: {path.name}")
            digest.update(chunk)
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
            raise PackSignatureError(f"Pack file changed during hashing: {path.name}")
        mode = stat.S_IMODE(before.st_mode)
        if mode not in {0o644, 0o755}:
            raise PackSignatureError(
                f"Pack file mode must be 0644 or 0755: {path.name}"
            )
        return digest.hexdigest(), total, mode
    finally:
        os.close(descriptor)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
