"""Pack v4 replacement for legacy file-inspect profile activation tests."""

from tests.legacy_authority_contracts import (
    assert_profile_resolver_requires_authority_snapshot,
    assert_retired_module_absent,
)


def test_file_inspect_activation_has_no_legacy_authority_imports() -> None:
    """File inspect activation is not assembled by deleted runtime modules."""
    for module_name in (
        "core_runtime.capability_binding_registration",
        "core_runtime.interface_registry",
        "core_runtime.startup_capability_bridge",
    ):
        assert_retired_module_absent(module_name)


def test_file_inspect_profile_requires_authority_snapshot() -> None:
    """The v4 resolver rejects activation without Kernel references."""
    assert_profile_resolver_requires_authority_snapshot()
