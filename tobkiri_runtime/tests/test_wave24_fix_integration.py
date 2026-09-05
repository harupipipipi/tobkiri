"""Pack v4 replacement for legacy function registration integration tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_legacy_function_registration_is_absent() -> None:
    """Registration fixes cannot reintroduce a global function authority."""
    assert_retired_module_absent("core_runtime.function_registry")
