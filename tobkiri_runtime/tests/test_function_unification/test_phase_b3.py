"""Pack v4 replacement for legacy capability dispatch tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_legacy_capability_dispatcher_is_absent() -> None:
    """Dispatch authority is owned by the v4 Host/Kernel boundary."""
    assert_retired_module_absent("core_runtime.capability_executor")
