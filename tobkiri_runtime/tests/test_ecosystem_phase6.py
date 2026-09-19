"""Pack v4 replacements for the retired runtime Ecosystem Registry tests.

Phase 6 originally exercised filesystem-discovered add-ons through the mutable
``Registry``.  Pack v4 deliberately has no such authority.  These tests keep
the old security questions (discovery, mutation, ordering, and cache state)
but assert the replacement boundary: a finite verified catalog, an
authority-bound Profile resolution, and a single-use Kernel lease.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog
from tests.legacy_authority_contracts import (
    assert_profile_resolver_rejects_unapproved_artifact,
    assert_profile_resolver_requires_authority_snapshot,
)
from tests.v4_batch_support import (
    assert_lease_is_single_use,
    assert_legacy_registry_fails_closed,
    assert_payload_mutations_denied,
    harness,
)


BUNDLE_ROOT = Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack" / "v4"


def _catalog() -> BundledCatalog:
    """Load the finite, digest-verified Pack v4 catalog."""
    return BundledCatalog.load(BUNDLE_ROOT)


class TestAddonLoading:
    """Filesystem add-on discovery is no longer a runtime authority."""

    def test_load_addon(self) -> None:
        assert_legacy_registry_fails_closed()
        catalog = _catalog()
        assert "defaultspack" in catalog.packs

    def test_addon_info(self) -> None:
        assert_legacy_registry_fails_closed()
        catalog = _catalog()
        assert all("artifact_digest" in item["pack"] for item in catalog.packs.values())


class TestAddonApplication:
    """Pack mutation and priority are replaced by captured v4 authority."""

    def test_addon_priority_order(self) -> None:
        assert_profile_resolver_requires_authority_snapshot()

    def test_addon_application(self, tmp_path: Path) -> None:
        assert_payload_mutations_denied(harness(tmp_path))

    def test_path_restriction(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        assert_payload_mutations_denied(h)
        assert h.scope.dimensions["path"] == ("/safe",)


class TestAddonDenyAll:
    """An unapproved artifact cannot become part of a v4 activation."""

    def test_deny_all_blocks_addons(self) -> None:
        assert_profile_resolver_rejects_unapproved_artifact()


class TestFilePatch:
    """File-like mutations remain bound to the captured request scope."""

    def test_file_patch_with_restriction(self, tmp_path: Path) -> None:
        h = harness(tmp_path)
        assert_payload_mutations_denied(h)
        assert h.scope.quotas["max_bytes"] == 1024


class TestAddonValidation:
    """Legacy add-on validation cannot be used to mint runtime authority."""

    def test_valid_addon(self) -> None:
        assert_legacy_registry_fails_closed()
        assert _catalog().profiles["defaults"]["state"] == "needs_resolution"

    def test_forbidden_operation(self) -> None:
        assert_profile_resolver_requires_authority_snapshot()


class TestAddonEnableDisable:
    """Enable/disable state is represented by an immutable activation."""

    def test_disable_addon(self) -> None:
        assert_profile_resolver_rejects_unapproved_artifact()

    def test_enable_addon(self, tmp_path: Path) -> None:
        assert_lease_is_single_use(harness(tmp_path))


class TestGetAllAddons:
    """The v4 catalog is finite and does not expose an add-on registry."""

    def test_get_all_addons(self) -> None:
        assert_legacy_registry_fails_closed()
        catalog = _catalog()
        assert len(catalog.packs) == len(set(catalog.packs))
        assert set(catalog.profiles) == {"defaults"}


class TestCacheManagement:
    """A stale runtime cache cannot be reloaded into a new authority graph."""

    def test_clear_cache(self) -> None:
        assert_legacy_registry_fails_closed()
        catalog = _catalog()
        assert catalog.root == BUNDLE_ROOT.resolve()
