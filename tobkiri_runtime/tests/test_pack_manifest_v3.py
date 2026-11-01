"""Pack v4 replacement for legacy manifest-v3 authority tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_legacy_interface_registry_is_absent_from_pack_manifest_loading() -> None:
    """Pack v3 manifest loading cannot publish a runtime interface authority."""
    assert_retired_module_absent("core_runtime.interface_registry")
