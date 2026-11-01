"""Pack v4 approval evidence replaces legacy kernel approval scan warnings."""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from ecosystem.defaultspack.domain.runtime_v4 import (
    BundledCatalog,
    ProfileResolutionDenied,
    resolve_default_profile,
)
from tests.legacy_authority_contracts import assert_retired_module_absent
from tests.conformance_support.packaged_profile import load_packaged_profile_catalog
from tests.v4_batch_support import authority_bindings_for_profile


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "ecosystem" / "defaultspack" / "v4"
SNAPSHOT = "sha256:" + "9" * 64


def _catalog() -> BundledCatalog:
    return load_packaged_profile_catalog()


def _approved(catalog: BundledCatalog) -> set[str]:
    return {str(item["pack"]["artifact_digest"]) for item in catalog.packs.values()}


def _bindings() -> dict[str, str]:
    return authority_bindings_for_profile(_catalog().profiles["defaults"])


def test_deleted_kernel_approval_scan_is_not_importable() -> None:
    assert_retired_module_absent("core_runtime.kernel_handlers_system")


def test_v4_profile_resolves_only_with_approved_artifacts() -> None:
    catalog = _catalog()
    resolved = resolve_default_profile(
        catalog,
        "defaults",
        approved_artifact_digests=_approved(catalog),
        authority_snapshot_digest=SNAPSHOT,
        authority_bindings=_bindings(),
        security_epoch=1,
    )
    assert resolved.profile["state"] == "resolved"


@pytest.mark.parametrize("pack_id", ["defaultspack", "rumi_file_inspect_pack"])
def test_v4_profile_denies_each_unapproved_pack(pack_id: str) -> None:
    catalog = _catalog()
    approved = _approved(catalog)
    approved.remove(catalog.packs[pack_id]["pack"]["artifact_digest"])
    with pytest.raises(ProfileResolutionDenied, match="not approved"):
        resolve_default_profile(
            catalog,
            "defaults",
            approved_artifact_digests=approved,
            authority_snapshot_digest=SNAPSHOT,
            authority_bindings=_bindings(),
            security_epoch=1,
        )


def test_v4_profile_does_not_accept_client_approval_marker() -> None:
    catalog = _catalog()
    with pytest.raises(ProfileResolutionDenied, match="Authority Kernel reference"):
        resolve_default_profile(
            catalog,
            "defaults",
            approved_artifact_digests=_approved(catalog),
            authority_snapshot_digest=SNAPSHOT,
            authority_bindings={},
            security_epoch=1,
        )


def test_v4_profile_rejects_duplicate_pack_identity() -> None:
    catalog = _catalog()
    duplicate = copy.deepcopy(catalog.packs["defaultspack"])
    duplicate["pack"]["id"] = "duplicate-defaultspack"
    duplicate["pack"]["artifact_digest"] = "sha256:" + "8" * 64
    duplicate_catalog = replace(
        catalog,
        packs={**catalog.packs, "duplicate-defaultspack": duplicate},
    )
    with pytest.raises(ProfileResolutionDenied, match="exactly once"):
        resolve_default_profile(
            duplicate_catalog,
            "defaults",
            approved_artifact_digests=_approved(duplicate_catalog),
            authority_snapshot_digest=SNAPSHOT,
            authority_bindings=_bindings(),
            security_epoch=1,
            additional_pack_ids=("duplicate-defaultspack",),
        )
