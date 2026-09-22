"""Pack identity tests after removal of the runtime Ecosystem Registry."""

from __future__ import annotations

import pytest

from backend_core.ecosystem.registry import LegacyRegistryUnavailable, Registry
from tests.v4_batch_support import assert_legacy_registry_fails_closed


def _legacy_load_is_rejected() -> None:
    """The old pack-id completer cannot become a runtime authority."""
    with pytest.raises(LegacyRegistryUnavailable, match="Pack v4"):
        Registry(ecosystem_dir="/tmp/legacy-pack-id-fixture").load_all_packs()


class TestPackIdAutoComplement:
    """Each former auto-complement branch now fails closed."""

    def test_both_present_no_change(self) -> None:
        _legacy_load_is_rejected()

    def test_pack_id_missing_github_identity(self) -> None:
        _legacy_load_is_rejected()

    def test_pack_id_missing_local_identity(self) -> None:
        _legacy_load_is_rejected()

    def test_pack_identity_missing_warns(self) -> None:
        assert_legacy_registry_fails_closed()

    def test_reserved_builtin_pack_id_must_match_directory_name(self) -> None:
        _legacy_load_is_rejected()
