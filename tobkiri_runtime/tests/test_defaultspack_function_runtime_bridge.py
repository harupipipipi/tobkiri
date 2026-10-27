"""Pack v4 replacement for the deleted Defaultspack function bridge tests."""

from tests.legacy_authority_contracts import (
    assert_profile_resolver_requires_authority_snapshot,
    assert_retired_module_absent,
)


def test_defaultspack_function_runtime_bridge_is_absent() -> None:
    """Defaultspack functions are no longer invoked through the old bridge."""
    assert_retired_module_absent("domain.function_runtime.bridge")


def test_defaultspack_function_profile_requires_kernel_authority() -> None:
    """The v4 Profile Resolver owns function-provider authority references."""
    assert_profile_resolver_requires_authority_snapshot()
