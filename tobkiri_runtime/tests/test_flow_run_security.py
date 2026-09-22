"""Pack v4 replacement for legacy flow-run security tests."""

from tests.legacy_authority_contracts import (
    assert_profile_resolver_requires_authority_snapshot,
    assert_retired_module_absent,
)


def test_flow_run_has_no_legacy_executor_authority() -> None:
    """A flow run cannot import the deleted executor."""
    assert_retired_module_absent("core_runtime.capability_executor")


def test_flow_run_requires_v4_authority_reference() -> None:
    """A v4 flow profile without Kernel authority is denied."""
    assert_profile_resolver_requires_authority_snapshot()
