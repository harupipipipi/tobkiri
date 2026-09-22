"""Pack v4 replacement for legacy permission security-flag tests."""

from tests.legacy_authority_contracts import (
    assert_legacy_service_fails_closed,
    assert_retired_module_absent,
)


def test_legacy_permission_authority_is_absent() -> None:
    """Security flags cannot revive the deleted permission manager."""
    assert_retired_module_absent("core_runtime.permission_manager")


def test_legacy_permission_service_fails_closed() -> None:
    """The explicit v4 boundary rejects the old security workflow."""
    assert_legacy_service_fails_closed()
