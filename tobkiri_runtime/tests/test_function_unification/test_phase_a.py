"""Pack v4 replacement for legacy FunctionRegistry unification tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_legacy_function_registry_is_absent() -> None:
    """Function identity is supplied by v4 artifacts, not a global registry."""
    assert_retired_module_absent("core_runtime.function_registry")
