"""Pack v4 replacement for legacy Search Home startup profile tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_search_home_does_not_import_legacy_interface_authority() -> None:
    """Search Home uses the captured profile rather than a global registry."""
    assert_retired_module_absent("core_runtime.interface_registry")
