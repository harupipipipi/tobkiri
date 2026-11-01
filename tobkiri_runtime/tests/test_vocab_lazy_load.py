"""Pack v4 has no runtime ComponentLifecycle vocabulary discovery path."""

from __future__ import annotations

from pathlib import Path

from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog
from tests.legacy_authority_contracts import assert_retired_module_absent
from tests.v4_bundle_support import assert_verified_pack_inventory


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "ecosystem" / "defaultspack" / "v4"


def test_component_lifecycle_authority_is_physically_absent() -> None:
    assert_retired_module_absent("core_runtime.component_lifecycle")


def test_v4_catalog_is_loaded_from_verified_artifacts() -> None:
    catalog = BundledCatalog.load(BUNDLE)
    assert_verified_pack_inventory(BUNDLE, catalog.packs)


def test_v4_pack_functions_have_explicit_contracts() -> None:
    for manifest in BundledCatalog.load(BUNDLE).packs.values():
        for function in manifest["functions"]:
            assert function["contract_revision_digest"]
            assert function["operations"]


def test_v4_catalog_does_not_scan_unlisted_vocab_files(tmp_path: Path) -> None:
    (tmp_path / "vocab.txt").write_text("untrusted", encoding="utf-8")
    catalog = BundledCatalog.load(BUNDLE)
    assert all("untrusted" not in str(item) for item in catalog.packs.values())


def test_v4_catalog_rejects_runtime_ecosystem_projection() -> None:
    assert not (ROOT / "ecosystem" / "defaultspack" / "ecosystem.json").exists()


def test_v4_inventory_is_deterministic_across_loads() -> None:
    first = BundledCatalog.load(BUNDLE)
    second = BundledCatalog.load(BUNDLE)
    assert first.packs == second.packs
