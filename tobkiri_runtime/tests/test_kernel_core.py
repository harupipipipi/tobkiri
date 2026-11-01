"""Pack v4 replacement for legacy KernelCore tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_legacy_kernel_core_is_absent() -> None:
    """The deleted KernelCore cannot be used as an alternate runtime root."""
    assert_retired_module_absent("core_runtime.kernel_core")
