"""Pack v4 replacement for legacy runtime policy tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_runtime_policy_has_no_legacy_interface_registry() -> None:
    """Policy enforcement cannot discover an alternate interface authority."""
    assert_retired_module_absent("core_runtime.interface_registry")
