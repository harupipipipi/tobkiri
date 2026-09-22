"""Repeated bundle reads retain byte, schema, and filesystem validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tobkiri_protocol import bundle_catalog
from tobkiri_protocol.bundle_catalog import BundleIntegrityError, BundledCatalog


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """Copy one real manifest into a minimal, digest-pinned bundle."""

    source = (
        Path(__file__).resolve().parents[1]
        / "ecosystem/defaultspack/v4/packs/defaultspack.pack.v4.json"
    )
    (tmp_path / "pack.json").write_bytes(source.read_bytes())
    _write_lock(tmp_path)
    bundle_catalog._validated_document.cache_clear()
    return tmp_path


def _write_lock(root: Path) -> None:
    raw = (root / "pack.json").read_bytes()
    lock = {
        "schema": "io.tobkiri.defaultspack-bundle-lock.v1",
        "entries": [
            {
                "path": "pack.json",
                "kind": "pack",
                "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            }
        ],
    }
    (root / "bundle.lock.json").write_text(json.dumps(lock), encoding="utf-8")


def test_repeated_reads_validate_once_and_do_not_share_mutable_documents(
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validate = bundle_catalog.validate_document
    calls: list[str] = []

    def record(raw: bytes, kind: str) -> dict:
        calls.append(kind)
        return validate(raw, kind)

    monkeypatch.setattr(bundle_catalog, "validate_document", record)
    first = BundledCatalog.load(bundle)
    first.packs["defaultspack"]["pack"]["id"] = "changed"
    second = BundledCatalog.load(bundle)
    assert second.packs["defaultspack"]["pack"]["id"] == "defaultspack"
    assert calls == ["pack"]


def test_warm_read_rejects_changed_bytes_even_with_same_size_and_timestamp(
    bundle: Path,
) -> None:
    import os

    BundledCatalog.load(bundle)
    path = bundle / "pack.json"
    before = path.stat()
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b"defaultspack", b"defaultspacx", 1))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(BundleIntegrityError, match="digest changed"):
        BundledCatalog.load(bundle)


def test_repinning_invalid_document_does_not_reuse_old_validation(bundle: Path) -> None:
    BundledCatalog.load(bundle)
    path = bundle / "pack.json"
    document = json.loads(path.read_bytes())
    document["pack"]["id"] = "invalid ID"
    path.write_text(json.dumps(document), encoding="utf-8")
    _write_lock(bundle)
    with pytest.raises(BundleIntegrityError, match="invalid pack document"):
        BundledCatalog.load(bundle)


@pytest.mark.parametrize("replacement", ["symlink", "missing"])
def test_warm_read_still_checks_the_artifact_path(bundle: Path, replacement: str) -> None:
    BundledCatalog.load(bundle)
    path = bundle / "pack.json"
    saved = bundle / "saved.json"
    path.rename(saved)
    if replacement == "symlink":
        try:
            path.symlink_to(saved)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows symlink creation requires Developer Mode or elevation")
            raise
    with pytest.raises((BundleIntegrityError, FileNotFoundError)):
        BundledCatalog.load(bundle)


def test_large_valid_documents_are_not_retained(bundle: Path) -> None:
    path = bundle / "pack.json"
    path.write_bytes(path.read_bytes() + b" " * bundle_catalog._MAX_CACHED_DOCUMENT_BYTES)
    _write_lock(bundle)
    BundledCatalog.load(bundle)
    BundledCatalog.load(bundle)
    assert bundle_catalog._validated_document.cache_info().currsize == 0
