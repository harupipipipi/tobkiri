"""Pack v4 replacement for legacy user-function execution tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_user_function_cannot_use_legacy_executor_or_registry() -> None:
    """User functions require verified v4 artifacts, not mutable authorities."""
    for module_name in (
        "core_runtime.capability_executor",
        "core_runtime.function_registry",
    ):
        assert_retired_module_absent(module_name)
