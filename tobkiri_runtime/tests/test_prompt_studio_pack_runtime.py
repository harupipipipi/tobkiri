"""Pack v4 replacement for legacy prompt-studio runtime tests."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_prompt_studio_cannot_bind_deleted_runtime_authorities() -> None:
    """Prompt composition cannot register old bindings or interfaces."""
    for module_name in (
        "core_runtime.capability_binding_registration",
        "core_runtime.interface_registry",
    ):
        assert_retired_module_absent(module_name)
