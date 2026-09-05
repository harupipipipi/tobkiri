"""Shared assertions for the verified Pack v4 bundle inventory."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Mapping

from tobkiri_protocol.canonical import strict_loads

_BUNDLE_LOCK_SCHEMA = "io.tobkiri.defaultspack-bundle-lock.v1"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _verified_pack_entries(bundle: Path) -> tuple[tuple[str, str], ...]:
    """Return the complete pack inventory declared by the verified bundle lock."""
    lock = strict_loads((bundle / "bundle.lock.json").read_bytes())
    assert isinstance(lock, dict)
    assert set(lock) == {"schema", "entries"}
    assert lock["schema"] == _BUNDLE_LOCK_SCHEMA
    entries = lock["entries"]
    assert isinstance(entries, list) and entries

    pack_entries: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    for entry in entries:
        assert isinstance(entry, dict)
        assert set(entry) == {"path", "kind", "digest"}
        relative_path = entry["path"]
        kind = entry["kind"]
        digest = entry["digest"]
        assert isinstance(relative_path, str)
        assert kind in {"pack", "base", "shell", "profile", "executable_catalog"}
        assert isinstance(digest, str) and _SHA256_RE.fullmatch(digest)
        assert relative_path not in seen_paths
        seen_paths.add(relative_path)
        if kind != "pack":
            continue
        assert relative_path.startswith("packs/")
        assert relative_path.endswith(".pack.v4.json")
        pack_entries.append((relative_path, digest))

    assert pack_entries
    expected_paths = {relative_path for relative_path, _ in pack_entries}
    actual_paths = {
        path.relative_to(bundle).as_posix()
        for path in (bundle / "packs").glob("*.pack.v4.json")
    }
    assert actual_paths == expected_paths
    return tuple(pack_entries)


def assert_verified_pack_inventory(
    bundle: Path, catalog_packs: Mapping[str, dict]
) -> None:
    """Verify every lock-pinned Pack artifact and its catalog identity."""
    pack_ids: set[str] = set()
    for relative_path, expected_digest in _verified_pack_entries(bundle):
        artifact_path = bundle / relative_path
        assert artifact_path.is_file()
        assert not artifact_path.is_symlink()
        artifact_bytes = artifact_path.read_bytes()
        actual_digest = f"sha256:{hashlib.sha256(artifact_bytes).hexdigest()}"
        assert actual_digest == expected_digest

        manifest = strict_loads(artifact_bytes)
        assert isinstance(manifest, dict)
        pack = manifest.get("pack")
        provenance = manifest.get("provenance")
        integrity = manifest.get("integrity")
        assert isinstance(pack, dict)
        assert isinstance(provenance, dict)
        assert isinstance(integrity, dict)
        pack_id = pack.get("id")
        assert isinstance(pack_id, str) and pack_id not in pack_ids
        assert _SHA256_RE.fullmatch(str(pack.get("artifact_digest")))
        assert _SHA256_RE.fullmatch(str(provenance.get("source_digest")))
        assert _SHA256_RE.fullmatch(str(integrity.get("source_identity")))
        assert isinstance(provenance.get("repository_commit"), str)
        assert isinstance(provenance.get("source_path"), str)
        assert catalog_packs.get(pack_id) == manifest
        pack_ids.add(pack_id)

    assert set(catalog_packs) == pack_ids
