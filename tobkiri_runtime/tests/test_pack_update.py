"""Pack v4 replacement for legacy permission-manager update tests."""

from tests.legacy_authority_contracts import (
    assert_profile_resolver_requires_authority_snapshot,
    assert_retired_module_absent,
)


def test_legacy_permission_manager_is_absent() -> None:
    """Pack updates cannot mint authority through the deleted manager."""
    assert_retired_module_absent("core_runtime.permission_manager")


def test_pack_update_requires_v4_authority_snapshot() -> None:
    """Profile updates without a Kernel snapshot are rejected."""
    assert_profile_resolver_requires_authority_snapshot()
