"""Legacy permissions.json expectations replaced by v4 authority snapshots."""

from __future__ import annotations

from pathlib import Path

import pytest

from ecosystem.defaultspack.domain.runtime_v4 import (
    ProfileResolutionDenied,
    resolve_default_profile,
)
from tests.conformance_support.packaged_profile import load_packaged_profile_catalog
from tests.legacy_authority_contracts import assert_retired_module_absent


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "ecosystem" / "defaultspack" / "v4"


def test_legacy_permission_manager_and_executor_are_absent() -> None:
    assert_retired_module_absent("core_runtime.permission_manager")
    assert_retired_module_absent("core_runtime.capability_executor")


def test_defaultspack_has_no_runtime_permissions_json() -> None:
    assert not (ROOT / "ecosystem" / "defaultspack" / "permissions.json").exists()


def test_profile_resolution_requires_authority_snapshot_and_bindings() -> None:
    catalog = load_packaged_profile_catalog()
    approved = {str(item["pack"]["artifact_digest"]) for item in catalog.packs.values()}
    with pytest.raises(ProfileResolutionDenied, match="Authority Kernel reference"):
        resolve_default_profile(
            catalog,
            "defaults",
            approved_artifact_digests=approved,
            authority_snapshot_digest="sha256:" + "9" * 64,
            authority_bindings={},
            security_epoch=1,
        )
