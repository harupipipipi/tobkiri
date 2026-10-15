"""Pack v4 migration contract for the retired binding authority."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_retired_binding_registration_authority_is_absent() -> None:
    """Binding registration is no longer a production runtime authority."""
    assert_retired_module_absent("core_runtime.capability_binding_registration")
