"""Pack v4 replacement for the legacy native file-inspect QA harness."""

from tests.legacy_authority_contracts import (
    assert_profile_resolver_rejects_unapproved_artifact,
    assert_retired_module_absent,
)


def test_native_file_inspect_uses_no_deleted_authority_modules() -> None:
    """Native QA cannot bind execution through the retired registries."""
    for module_name in (
        "core_runtime.capability_binding_registration",
        "core_runtime.interface_registry",
    ):
        assert_retired_module_absent(module_name)


def test_native_file_inspect_profile_rejects_unapproved_artifact() -> None:
    """The Profile Resolver fails closed when one artifact is not approved."""
    assert_profile_resolver_rejects_unapproved_artifact()
