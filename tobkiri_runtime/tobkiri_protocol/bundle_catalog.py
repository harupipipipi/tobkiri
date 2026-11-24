"""Finite, digest-verified Protocol bundle input shared by Host and generators."""

from __future__ import annotations

import copy
import hashlib
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping

from .canonical import canonical_digest, strict_loads
from .errors import ProtocolError, SchemaValidationError
from .validation import validate_document

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BUNDLE_SCHEMA = "io.tobkiri.defaultspack-bundle-lock.v1"
_MAX_CACHED_DOCUMENT_BYTES = 64 * 1024
_MAX_LARGE_DOCUMENT_BYTES = 256 * 1024
_MAX_LARGE_CACHE_BYTES = 2 * 1024 * 1024
_MAX_LARGE_CACHE_ENTRIES = 16
_LARGE_CACHE: OrderedDict[tuple[bytes, str], dict[str, Any]] = OrderedDict()
_LARGE_CACHE_LOCK = RLock()
_LARGE_CACHE_BYTES = 0


@lru_cache(maxsize=512)
def _validated_document(raw: bytes, kind: str) -> dict[str, Any]:
    """Memoize only pure validation of bounded, exact serialized documents.

    Paths, bundle membership, digests, and authority are checked by callers
    on every load. The cache owns its result; callers must copy it before use.
    """

    return validate_document(raw, kind)


def _clear_large_validation_cache() -> None:
    """Clear pure validation results without replacing a live thread mutex."""
    global _LARGE_CACHE_BYTES
    with _LARGE_CACHE_LOCK:
        _LARGE_CACHE.clear()
        _LARGE_CACHE_BYTES = 0


def _reset_large_validation_cache_after_fork() -> None:
    """Drop pure cached data and any mutex owned by a vanished parent thread."""
    global _LARGE_CACHE, _LARGE_CACHE_LOCK, _LARGE_CACHE_BYTES
    _LARGE_CACHE = OrderedDict()
    _LARGE_CACHE_LOCK = RLock()
    _LARGE_CACHE_BYTES = 0


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_large_validation_cache_after_fork)


def _validated_large_document(raw: bytes, kind: str) -> dict[str, Any]:
    """Reuse pure exact-byte validation within finite raw-byte and entry bounds.

    Schema definitions and format validators are fixed process inputs, as for
    the existing small-document cache. No path, digest, signature, activation
    or Authority decision is cached. The caller must deepcopy this private
    cache-owned value before exposing it.
    """
    global _LARGE_CACHE_BYTES
    if len(raw) <= _MAX_CACHED_DOCUMENT_BYTES:
        return _validated_document(raw, kind)
    if (len(raw) > min(_MAX_LARGE_DOCUMENT_BYTES, _MAX_LARGE_CACHE_BYTES)
            or _MAX_LARGE_CACHE_ENTRIES < 1):
        return validate_document(raw, kind)
    key = (raw, kind)
    with _LARGE_CACHE_LOCK:
        cached = _LARGE_CACHE.get(key)
        if cached is not None:
            _LARGE_CACHE.move_to_end(key)
            return cached
    # Independent concurrent misses may validate twice. Neither exceptions nor
    # partially validated values are ever retained.
    document = validate_document(raw, kind)
    with _LARGE_CACHE_LOCK:
        cached = _LARGE_CACHE.get(key)
        if cached is not None:
            _LARGE_CACHE.move_to_end(key)
            return cached
        while _LARGE_CACHE and (
            len(_LARGE_CACHE) >= _MAX_LARGE_CACHE_ENTRIES
            or _LARGE_CACHE_BYTES + len(raw) > _MAX_LARGE_CACHE_BYTES
        ):
            evicted_key, _ = _LARGE_CACHE.popitem(last=False)
            _LARGE_CACHE_BYTES -= len(evicted_key[0])
        _LARGE_CACHE[key] = document
        _LARGE_CACHE_BYTES += len(raw)
    return document


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
            lock_bytes = lock_path.read_bytes()
        except (OSError, ProtocolError) as exc:
            raise BundleIntegrityError(f"cannot read bundle lock: {exc}") from exc

        def read_native_entry(index: int, relative: str, reread: bool) -> bytes:
            # Keep the executable-catalog pin read identical to the native
            # loader's original (root / path).read_bytes() call: it did not
            # resolve or repeat the symlink walk on this second read.
            if reread:
                return (root / relative).read_bytes()
            candidate = (root / relative).resolve(strict=True)
            if candidate == root or root not in candidate.parents:
                raise BundleIntegrityError(f"bundle entry escapes root: {relative}")
            relative_path = Path(relative)
            current = root
            for part in relative_path.parts:
                current /= part
                if current.is_symlink():
                    raise BundleIntegrityError(f"bundle entry contains a symlink: {relative}")
            return candidate.read_bytes()

        def resolve_native_artifact_root() -> Path | None:
            return (
                artifact_root.resolve(strict=True)
                if artifact_root
                else (
                    (root.parent / "platform-artifacts").resolve(strict=True)
                    if (root.parent / "platform-artifacts").is_dir()
                    else None
                )
            )

        return cls.load_from_reader(
            lock_bytes,
            entry_reader=read_native_entry,
            root=root,
            artifact_root_resolver=resolve_native_artifact_root,
        )

    @classmethod
    def load_from_reader(
        cls,
        lock_bytes: bytes,
        *,
        entry_reader: Callable[[int, str, bool], bytes],
        root: Any,
        artifact_root_resolver: Callable[[], Any],
    ) -> "BundledCatalog":
        """Validate bundle DATA supplied by an ordinal, application-owned reader.

        This is a data-only seam: ``root`` is retained as a presentation/data
        view and is never converted to or used as an OS path. ``entry_reader``
        is a private application DATA lookup, not a host import or authority
        source. It is called once per lock entry occurrence, in lock order, and
        again for executable-catalog pin checks with ``reread=True``. It must
        preserve occurrence identity instead of looking entries up by
        normalized path.
        Artifact-root resolution is deferred until every document and pin has
        passed validation.
        """
        try:
            lock = strict_loads(lock_bytes)
        except ProtocolError as exc:
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
        executable_lock_entries: dict[str, tuple[int, str, str]] = {}
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
            raw = entry_reader(index, relative, False)
            actual_digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
            if actual_digest != expected_digest:
                raise BundleIntegrityError(
                    f"bundle artifact digest changed: {relative} "
                    f"({actual_digest} != {expected_digest})"
                )
            try:
                document = (
                    copy.deepcopy(_validated_document(raw, kind))
                    if len(raw) <= _MAX_CACHED_DOCUMENT_BYTES
                    else copy.deepcopy(_validated_large_document(raw, kind))
                    if len(raw) <= _MAX_LARGE_DOCUMENT_BYTES
                    else validate_document(raw, kind)
                )
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
                executable_lock_entries[identity] = (index, relative, expected_digest)
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
            catalog_index, catalog_path, catalog_lock_digest = executable_lock_entries[pack_id]
            catalog_raw = entry_reader(catalog_index, catalog_path, True)
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
            artifact_root=artifact_root_resolver(),
            executable_catalogs=collections["executable_catalog"],
        )
