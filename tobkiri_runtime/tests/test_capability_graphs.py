"""Pack v4 migration contract for retired graph authorities."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_legacy_graph_compilers_and_loaders_are_absent() -> None:
    """Capability graph discovery cannot bypass the resolved v4 plan."""
    for module_name in (
        "core_runtime.binding_handlers",
        "core_runtime.capability_graph_compiler",
        "core_runtime.capability_graph_loader",
    ):
        assert_retired_module_absent(module_name)
