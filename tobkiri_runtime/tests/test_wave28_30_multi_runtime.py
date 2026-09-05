"""Pack v4 replacement for legacy multi-runtime authority tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_multi_runtime_legacy_authorities_are_absent() -> None:
    """Multiple runtimes cannot share the deleted global executor/registry."""
    for module_name in (
        "core_runtime.capability_executor",
        "core_runtime.function_registry",
    ):
        assert_retired_module_absent(module_name)
