"""Pack v4 replacement for the legacy end-to-end capability chain."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_legacy_capability_chain_authorities_are_absent() -> None:
    """An old executor/registry chain cannot be reintroduced through imports."""
    for module_name in (
        "core_runtime.capability_executor",
        "core_runtime.function_registry",
    ):
        assert_retired_module_absent(module_name)
