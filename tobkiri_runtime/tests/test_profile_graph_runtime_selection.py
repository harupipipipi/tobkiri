"""Pack v4 replacement for legacy startup graph selection tests."""

from tests.legacy_authority_contracts import (
    assert_profile_resolver_requires_authority_snapshot,
    assert_retired_module_absent,
)


def test_startup_graph_bridge_is_absent() -> None:
    """Runtime selection cannot mutate authority through a legacy bridge."""
    assert_retired_module_absent("core_runtime.startup_capability_bridge")


def test_profile_graph_selection_requires_kernel_authority() -> None:
    """The v4 Profile Resolver denies a graph without Kernel references."""
    assert_profile_resolver_requires_authority_snapshot()
