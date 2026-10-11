"""Pack v4 migration contract for retired node discovery."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_legacy_ecosystem_node_registry_is_absent() -> None:
    """Node discovery is supplied by the captured Profile/ResolvedPlan."""
    assert_retired_module_absent("core_runtime.ecosystem_nodes")
