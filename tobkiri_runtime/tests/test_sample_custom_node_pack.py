"""Pack v4 replacement for the legacy custom-node pack harness."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_custom_node_pack_cannot_use_deleted_graph_authorities() -> None:
    """Untrusted node packs cannot reintroduce old graph/runtime registries."""
    for module_name in (
        "core_runtime.capability_binding_registration",
        "core_runtime.capability_graph_compiler",
        "core_runtime.capability_graph_loader",
        "core_runtime.ecosystem_nodes",
        "core_runtime.interface_registry",
        "core_runtime.profile_loader",
    ):
        assert_retired_module_absent(module_name)
