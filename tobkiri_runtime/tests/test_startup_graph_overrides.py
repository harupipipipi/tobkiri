"""Pack v4 replacement for legacy startup graph override tests."""

from tests.legacy_authority_contracts import (
    assert_profile_resolver_requires_authority_snapshot,
    assert_retired_module_absent,
)


def test_startup_graph_override_bridge_is_absent() -> None:
    """Overrides cannot widen a resolved v4 activation through a bridge."""
    assert_retired_module_absent("core_runtime.startup_capability_bridge")


def test_startup_graph_override_requires_authority_snapshot() -> None:
    """The Profile Resolver rejects an activation without Kernel references."""
    assert_profile_resolver_requires_authority_snapshot()
