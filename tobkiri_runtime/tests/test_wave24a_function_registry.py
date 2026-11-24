"""Pack v4 replacement for the retired FunctionRegistry test group."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_function_registry_is_physically_retired() -> None:
    """Exact Function principals replace the old mutable registry."""
    assert_retired_module_absent("core_runtime.function_registry")
