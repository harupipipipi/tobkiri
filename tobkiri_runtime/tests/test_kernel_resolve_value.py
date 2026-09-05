"""Pack v4 replacement for legacy Kernel value resolution tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_legacy_kernel_value_resolver_is_absent() -> None:
    """A deleted KernelCore cannot resolve values outside the v4 plan."""
    assert_retired_module_absent("core_runtime.kernel_core")
