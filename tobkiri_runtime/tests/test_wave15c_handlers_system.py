"""Pack v4 replacement for legacy system-handler tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_legacy_system_handler_authority_is_absent() -> None:
    """System handlers cannot become a second Kernel authority."""
    assert_retired_module_absent("core_runtime.kernel_handlers_system")
