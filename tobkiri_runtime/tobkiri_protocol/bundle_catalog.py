"""Finite, digest-verified Protocol bundle input shared by Host and generators."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .canonical import canonical_digest, strict_loads
from .errors import ProtocolError, SchemaValidationError
from .validation import validate_document

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BUNDLE_SCHEMA = "io.tobkiri.defaultspack-bundle-lock.v1"


class DefaultProfileV4Error(RuntimeError):
    """Compatibility base error for the Profile v4 boundary."""


class BundleIntegrityError(DefaultProfileV4Error):
    """Raised when the finite bundled inventory is invalid or has drifted."""


@dataclass(frozen=True)
class BundledCatalog:
    """Finite, digest-verified collection of bundled v4 documents."""

    root: Path
    packs: Mapping[str, Mapping[str, Any]]
    bases: Mapping[str, Mapping[str, Any]]
    shells: Mapping[str, Mapping[str, Any]]
    profiles: Mapping[str, Mapping[str, Any]]
    artifact_root: Path | None = None
    executable_catalogs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path, *, artifact_root: Path | None = None) -> "BundledCatalog":
        """Load only files named by ``bundle.lock.json`` and verify every byte."""
        requested_root = Path(root)
        if requested_root.is_symlink():
            raise BundleIntegrityError("bundle root must not be a symlink")
        root = requested_root.resolve(strict=True)
        lock_path = root / "bundle.lock.json"
        if lock_path.is_symlink():
            raise BundleIntegrityError("bundle lock must not be a symlink")
        try:
            lock = strict_loads(lock_path.read_bytes())
        except (OSError, ProtocolError) as exc:
            raise BundleIntegrityError(f"cannot read bundle lock: {exc}") from exc
        if not isinstance(lock, dict) or set(lock) != {"schema", "entries"}:
            raise BundleIntegrityError("bundle lock has unknown or missing fields")
        if lock.get("schema") != _BUNDLE_SCHEMA:
            raise BundleIntegrityError("bundle lock schema is not supported")
        entries = lock.get("entries")
        if not isinstance(entries, list) or not entries:
            raise BundleIntegrityError("bundle lock entries must be a non-empty array")

        collections: dict[str, dict[str, Mapping[str, Any]]] = {
            "pack": {},
            "base": {},
            "shell": {},
            "profile": {},
            "executable_catalog": {},
        }
        identity_fields = {
            "pack": ("pack", "id"),
            "base": (None, "pack_id"),
            "shell": (None, "provider_id"),
            "profile": (None, "profile_id"),
            "executable_catalog": (None, "pack_id"),
        }
        seen_paths: set[str] = set()
        executable_lock_entries: dict[str, tuple[str, str]] = {}
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or set(entry) != {"path", "kind", "digest"}:
                raise BundleIntegrityError(f"bundle entry {index} has invalid fields")
            relative = entry.get("path")
            kind = entry.get("kind")
            expected_digest = entry.get("digest")
            if not isinstance(relative, str) or not relative or relative in seen_paths:
                raise BundleIntegrityError(f"bundle entry {index} has an invalid path")
            if kind not in collections:
                raise BundleIntegrityError(f"bundle entry {relative} has an invalid kind")
            if (
                not isinstance(expected_digest, str)
                or _DIGEST_RE.fullmatch(expected_digest) is None
            ):
                raise BundleIntegrityError(f"bundle entry {relative} has an invalid digest")
            candidate = (root / relative).resolve(strict=True)
            if candidate == root or root not in candidate.parents:
                raise BundleIntegrityError(f"bundle entry escapes root: {relative}")
            relative_path = Path(relative)
            current = root
            for part in relative_path.parts:
                current /= part
                if current.is_symlink():
                    raise BundleIntegrityError(f"bundle entry contains a symlink: {relative}")
            raw = candidate.read_bytes()
            actual_digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
            if actual_digest != expected_digest:
                raise BundleIntegrityError(
                    f"bundle artifact digest changed: {relative} "
                    f"({actual_digest} != {expected_digest})"
                )
            try:
                document = validate_document(raw, kind)
            except SchemaValidationError as exc:
                raise BundleIntegrityError(f"invalid {kind} document {relative}: {exc}") from exc
            if kind in {"base", "shell"}:
                expected_revision = canonical_digest(
                    {key: value for key, value in document.items() if key != "definition_revision"}
                )
                if document["definition_revision"] != expected_revision:
                    raise BundleIntegrityError(
                        f"{kind} definition revision is stale or tampered: {relative}"
                    )
            parent_field, identity_field = identity_fields[kind]
            identity_source = document.get(parent_field) if parent_field else document
            identity = (
                identity_source.get(identity_field) if isinstance(identity_source, dict) else None
            )
            if not isinstance(identity, str) or identity in collections[kind]:
                raise BundleIntegrityError(f"duplicate or missing {kind} identity: {identity!r}")
            collections[kind][identity] = document
            if kind == "executable_catalog":
                executable_lock_entries[identity] = (relative, expected_digest)
            seen_paths.add(relative)
        for pack_id, executable in collections["executable_catalog"].items():
            manifest = collections["pack"].get(pack_id)
            if manifest is None:
                raise BundleIntegrityError(
                    f"executable catalog has no bundled Pack manifest: {pack_id}"
                )
            if executable["source_identity"] != manifest["integrity"]["source_identity"]:
                raise BundleIntegrityError(
                    f"executable catalog source identity is stale: {pack_id}"
                )
            expected_catalog_digest = canonical_digest(
                {key: value for key, value in executable.items() if key != "catalog_digest"}
            )
            if executable["catalog_digest"] != expected_catalog_digest:
                raise BundleIntegrityError(f"executable catalog digest is stale: {pack_id}")
            catalog_entries = [
                item for item in manifest["artifacts"] if item["path"] == "executables.v4.json"
            ]
            if len(catalog_entries) != 1:
                raise BundleIntegrityError(
                    f"Pack manifest does not pin executable catalog: {pack_id}"
                )
            catalog_path, catalog_lock_digest = executable_lock_entries[pack_id]
            catalog_raw = (root / catalog_path).read_bytes()
            catalog_raw_digest = 'sha256:' + hashlib.sha256(catalog_raw).hexdigest()
            if (
                catalog_entries[0]["digest"] != catalog_raw_digest
                or catalog_lock_digest != catalog_raw_digest
            ):
                raise BundleIntegrityError(
                    f"Pack executable catalog artifact pin is stale: {pack_id}"
                )
        return cls(
            root=root,
            packs=collections["pack"],
            bases=collections["base"],
            shells=collections["shell"],
            profiles=collections["profile"],
            artifact_root=(
                artifact_root.resolve(strict=True)
                if artifact_root
                else (
                    (root.parent / "platform-artifacts").resolve(strict=True)
                    if (root.parent / "platform-artifacts").is_dir()
                    else None
                )
            ),
            executable_catalogs=collections["executable_catalog"],
        )
