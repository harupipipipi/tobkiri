"""Pack v4 replacement for legacy startup profile workspace integration tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_startup_workspace_has_no_legacy_profile_registry_dependency() -> None:
    """Workspace activation cannot import the deleted interface registry."""
    assert_retired_module_absent("core_runtime.interface_registry")
