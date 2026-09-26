"""v4 artifact digests replace the legacy Registry JSON scan guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from ecosystem.defaultspack.domain.runtime_v4 import BundleIntegrityError, BundledCatalog
from tests.v4_batch_support import assert_legacy_registry_fails_closed
from tests.v4_bundle_support import assert_verified_pack_inventory


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "ecosystem" / "defaultspack" / "v4"


def test_legacy_registry_json_guard_is_not_importable() -> None:
    assert_legacy_registry_fails_closed()


def test_v4_catalog_validates_each_manifest_digest() -> None:
    catalog = BundledCatalog.load(BUNDLE)
    assert_verified_pack_inventory(BUNDLE, catalog.packs)
    assert all(item["pack"]["artifact_digest"].startswith("sha256:") for item in catalog.packs.values())


def test_v4_catalog_rejects_manifest_byte_drift(tmp_path: Path) -> None:
    copied = tmp_path / "v4"
    import shutil

    shutil.copytree(BUNDLE, copied)
    path = copied / "packs" / "defaultspack.pack.v4.json"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(BundleIntegrityError, match="digest changed"):
        BundledCatalog.load(copied)


def test_v4_artifact_index_is_present_for_every_pack() -> None:
    for pack in (BUNDLE / "packs").glob("*.pack.v4.json"):
        assert pack.is_file()
