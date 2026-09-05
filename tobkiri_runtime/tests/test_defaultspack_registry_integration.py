"""Pack v4 replacement for the legacy registry integration tests."""

from tests.legacy_authority_contracts import assert_legacy_service_fails_closed


def test_legacy_registry_execution_path_fails_closed() -> None:
    """Legacy registry-driven execution is rejected at the explicit boundary."""
    assert_legacy_service_fails_closed()
