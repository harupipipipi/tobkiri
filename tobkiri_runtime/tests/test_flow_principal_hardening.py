"""Pack v4 replacement for legacy flow principal hardening tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_legacy_flow_executor_is_absent() -> None:
    """Flow execution cannot fall back to the deleted capability executor."""
    assert_retired_module_absent("core_runtime.capability_executor")
