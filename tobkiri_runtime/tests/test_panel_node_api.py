"""Pack v4 replacement for the legacy panel node API tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_panel_node_discovery_authorities_are_absent() -> None:
    """Panel nodes must come from the resolved v4 inventory."""
    for module_name in (
        "core_runtime.ecosystem_nodes",
        "core_runtime.profile_loader",
    ):
        assert_retired_module_absent(module_name)
