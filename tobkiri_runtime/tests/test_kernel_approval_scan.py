"""Pack v4 replacement for the deleted Kernel approval scan tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_legacy_kernel_approval_handlers_are_absent() -> None:
    """Approval scanning cannot become a second runtime authority."""
    assert_retired_module_absent("core_runtime.kernel_handlers_system")
