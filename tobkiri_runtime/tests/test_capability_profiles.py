"""Pack v4 migration contract for retired profile discovery."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_legacy_profile_loader_is_absent() -> None:
    """A runtime profile cannot be rebuilt through the deleted loader."""
    assert_retired_module_absent("core_runtime.profile_loader")
