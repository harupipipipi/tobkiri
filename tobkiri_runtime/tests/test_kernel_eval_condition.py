"""Pack v4 replacement for legacy Kernel condition evaluation tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_legacy_kernel_condition_authority_is_absent() -> None:
    """Conditions are evaluated within resolved v4 records, not legacy Kernel."""
    assert_retired_module_absent("core_runtime.kernel")
