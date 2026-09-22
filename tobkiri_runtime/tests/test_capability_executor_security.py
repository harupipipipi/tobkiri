"""Fail-closed replacement for legacy capability executor security tests."""

from tests.legacy_authority_contracts import (
    assert_legacy_service_fails_closed,
    assert_retired_module_absent,
)


def test_capability_executor_authority_is_physically_retired() -> None:
    """No old executor can become an execution authority by import."""
    assert_retired_module_absent("core_runtime.capability_executor")


def test_legacy_authority_service_rejects_execution() -> None:
    """The explicit tombstone rejects the former approval/execution workflow."""
    assert_legacy_service_fails_closed()
