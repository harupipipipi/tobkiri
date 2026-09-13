"""Pack v4 replacement for legacy core-pack handler scan tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_core_pack_handler_scan_has_no_global_function_registry() -> None:
    """Core Pack functions are bound by verified v4 artifacts."""
    assert_retired_module_absent("core_runtime.function_registry")
