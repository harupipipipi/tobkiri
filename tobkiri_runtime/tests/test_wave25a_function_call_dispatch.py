"""Pack v4 replacement for legacy function-call dispatch tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_function_call_dispatcher_is_physically_retired() -> None:
    """Function calls are dispatched through the v4 Host/Kernel boundary."""
    assert_retired_module_absent("core_runtime.capability_executor")
