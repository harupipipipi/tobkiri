"""v4 registration contracts replacing CapabilityExecutor dispatch tests."""

from __future__ import annotations

from pathlib import Path

from core_runtime.authority.v4 import authority_digest
from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog
from tests.legacy_authority_contracts import assert_retired_module_absent
from tests.v4_bundle_support import assert_verified_pack_inventory


ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "ecosystem" / "defaultspack" / "v4"


def test_v4_registration_is_catalog_backed() -> None:
    catalog = BundledCatalog.load(BUNDLE)
    assert_verified_pack_inventory(BUNDLE, catalog.packs)
    assert all(
        manifest["functions"]
        for manifest in catalog.packs.values()
        if manifest["pack"]["kind"] not in {"base", "shell"}
    )


def test_v4_registration_keeps_implementation_and_contract_digests() -> None:
    for manifest in BundledCatalog.load(BUNDLE).packs.values():
        for function in manifest["functions"]:
            assert function["implementation_digest"].startswith("sha256:")
            assert function["contract_revision_digest"].startswith("sha256:")


def test_v4_registration_rejects_duplicate_principal_material() -> None:
    material = {"pack": "defaultspack", "function": "conversation", "operation": "complete"}
    assert authority_digest(material) == authority_digest(dict(material))
    assert authority_digest(material) != authority_digest({**material, "operation": "admin"})


def test_v4_registration_has_no_capability_executor_compatibility_path() -> None:
    assert_retired_module_absent("core_runtime.capability_executor")
    assert_retired_module_absent("core_runtime.function_registry")


def test_v4_registration_is_not_mutable_global_state() -> None:
    first = BundledCatalog.load(BUNDLE)
    second = BundledCatalog.load(BUNDLE)
    assert first.packs.keys() == second.packs.keys()
