"""Pack v4 inventory and route exactness replacing legacy Registry scans."""

from __future__ import annotations

from pathlib import Path

import pytest

from ecosystem.defaultspack.domain.runtime_v4 import (
    BundledCatalog,
    ProfileResolutionDenied,
    resolve_default_profile,
)
from tests.v4_batch_support import (
    assert_legacy_registry_fails_closed,
    authority_bindings_for_profile,
)
from tests.conformance_support.packaged_profile import load_packaged_profile_catalog


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "ecosystem" / "defaultspack" / "v4"
SNAPSHOT = "sha256:" + "9" * 64
def _catalog() -> BundledCatalog:
    return load_packaged_profile_catalog()


def _approved(catalog: BundledCatalog) -> set[str]:
    return {str(item["pack"]["artifact_digest"]) for item in catalog.packs.values()}


def test_legacy_registry_is_not_a_runtime_inventory() -> None:
    assert_legacy_registry_fails_closed()


def test_v4_catalog_has_no_filesystem_discovery_gap() -> None:
    catalog = _catalog()
    for manifest in catalog.packs.values():
        assert manifest["pack"]["artifact_digest"].startswith("sha256:")
        if manifest["pack"]["kind"] not in {"base", "shell"}:
            assert manifest["functions"]
            assert manifest["contracts"]


def test_v4_function_ids_are_manifest_declared_and_unique() -> None:
    identities = []
    for manifest in _catalog().packs.values():
        for function in manifest["functions"]:
            identities.append((manifest["pack"]["id"], function["id"]))
            assert function["id"]
            assert function["implementation_digest"].startswith("sha256:")
    assert len(identities) == len(set(identities))


def test_v4_profile_resolves_exact_effective_set() -> None:
    catalog = _catalog()
    resolved = resolve_default_profile(
        catalog,
        "defaults",
        approved_artifact_digests=_approved(catalog),
        authority_snapshot_digest=SNAPSHOT,
        authority_bindings=authority_bindings_for_profile(
            catalog.profiles["defaults"]
        ),
        security_epoch=1,
    )
    expected_effective_set = [
        resolved.profile["base"]["pack_id"],
        resolved.profile["shell"]["pack_id"],
        *(item["pack_id"] for item in resolved.profile["packs"]),
    ]
    assert [item["identity"] for item in resolved.lock["effective_set"]] == (
        expected_effective_set
    )


def test_v4_profile_rejects_unapproved_manifest(tmp_path: Path) -> None:
    del tmp_path
    catalog = _catalog()
    approved = _approved(catalog)
    approved.remove(catalog.packs["rumi_file_inspect_pack"]["pack"]["artifact_digest"])
    with pytest.raises(ProfileResolutionDenied, match="not approved"):
        resolve_default_profile(
            catalog,
            "defaults",
            approved_artifact_digests=approved,
            authority_snapshot_digest=SNAPSHOT,
            authority_bindings=authority_bindings_for_profile(
                catalog.profiles["defaults"]
            ),
            security_epoch=1,
        )


def test_v4_profile_rejects_missing_authority_reference() -> None:
    with pytest.raises(ProfileResolutionDenied, match="Authority Kernel reference"):
        resolve_default_profile(
            _catalog(),
            "defaults",
            approved_artifact_digests=_approved(_catalog()),
            authority_snapshot_digest=SNAPSHOT,
            authority_bindings={},
            security_epoch=1,
        )


def test_v4_plan_routes_are_exactly_the_selected_bindings() -> None:
    catalog = _catalog()
    resolved = resolve_default_profile(
        catalog,
        "defaults",
        approved_artifact_digests=_approved(catalog),
        authority_snapshot_digest=SNAPSHOT,
        authority_bindings=authority_bindings_for_profile(
            catalog.profiles["defaults"]
        ),
        security_epoch=1,
    )
    assert len(resolved.plan["bindings"]) == len(resolved.profile["requested_edges"])
    assert resolved.lock["plan_digest"] == resolved.plan["plan_digest"]
