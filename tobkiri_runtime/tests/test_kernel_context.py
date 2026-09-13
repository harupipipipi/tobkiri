"""Pack v4 replacement for legacy Kernel context builder tests."""

from tests.legacy_authority_contracts import (
    assert_profile_resolver_requires_authority_snapshot,
    assert_retired_module_absent,
)


def test_legacy_kernel_context_builder_is_absent() -> None:
    """Invocation context comes from the captured v4 activation."""
    assert_retired_module_absent("core_runtime.kernel_context_builder")


def test_profile_resolution_rejects_missing_kernel_context() -> None:
    """The Profile Resolver requires the Host's Authority Kernel snapshot."""
    assert_profile_resolver_requires_authority_snapshot()
