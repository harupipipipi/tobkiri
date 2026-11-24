"""Pack v4 migration contract for removed DI authority services."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_removed_di_authorities_are_not_importable() -> None:
    """The container cannot expose deleted registry/lifecycle authorities."""
    for module_name in (
        "core_runtime.interface_registry",
        "core_runtime.component_lifecycle",
    ):
        assert_retired_module_absent(module_name)
