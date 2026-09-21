"""Pack v4 replacement for legacy permission default tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_legacy_permission_manager_is_not_a_default_authority() -> None:
    """Default permissions cannot be minted by the deleted manager."""
    assert_retired_module_absent("core_runtime.permission_manager")
