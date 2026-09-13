"""Pack v4 function identity checks replacing the kernel handler manifest."""

from __future__ import annotations

from pathlib import Path

from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog
from tests.legacy_authority_contracts import assert_retired_module_absent
from tests.v4_bundle_support import assert_verified_pack_inventory


ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "ecosystem" / "defaultspack" / "v4"


def _catalog() -> BundledCatalog:
    return BundledCatalog.load(BUNDLE)


def test_v4_catalog_is_the_single_function_inventory() -> None:
    catalog = _catalog()
    assert catalog.packs
    assert_verified_pack_inventory(BUNDLE, catalog.packs)


def test_v4_functions_have_exact_identity_fields() -> None:
    for manifest in _catalog().packs.values():
        for function in manifest["functions"]:
            assert function["id"]
            assert function["implementation_digest"].startswith("sha256:")
            assert function["contract_revision_digest"].startswith("sha256:")
            assert function["operations"]


def test_v4_function_operations_are_declared_by_contract_catalog() -> None:
    for manifest in _catalog().packs.values():
        contracts = {
            item["revision_digest"]: item for item in manifest["contracts"]
        }
        for function in manifest["functions"]:
            contract = contracts[function["contract_revision_digest"]]
            assert set(function["operations"]) <= set(contract["operations"])


def test_v4_inventory_has_no_mutable_handler_authority_module() -> None:
    assert_retired_module_absent("core_runtime.kernel")
    assert_retired_module_absent("core_runtime.kernel_handlers_system")


def test_v4_catalog_has_no_duplicate_function_identity() -> None:
    identities = []
    for manifest in _catalog().packs.values():
        identities.extend(
            (manifest["pack"]["id"], function["id"])
            for function in manifest["functions"]
        )
    assert len(identities) == len(set(identities))
