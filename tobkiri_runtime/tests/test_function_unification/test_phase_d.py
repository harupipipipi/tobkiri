"""Physical retirement and fail-closed tests for the removed v3 authority."""

from __future__ import annotations

from tests.legacy_authority_contracts import (
    assert_legacy_service_fails_closed,
    assert_profile_resolver_requires_authority_snapshot,
    assert_retired_module_absent,
)


RETIRED = (
    "core_runtime.capability_executor",
    "core_runtime.function_registry",
    "core_runtime.interface_registry",
    "core_runtime.kernel",
    "core_runtime.kernel_core",
    "core_runtime.kernel_handlers_system",
    "core_runtime.permission_manager",
    "core_runtime.component_lifecycle",
    "core_runtime.ecosystem_nodes",
)


def test_all_pre_v4_authority_modules_are_physically_absent() -> None:
    for module_name in RETIRED:
        assert_retired_module_absent(module_name)


def test_legacy_authority_service_is_not_a_fallback() -> None:
    assert_legacy_service_fails_closed()


def test_profile_resolver_replaces_legacy_manifest_registry() -> None:
    assert_profile_resolver_requires_authority_snapshot()


def test_v4_migration_does_not_reintroduce_legacy_alias_modules() -> None:
    assert_retired_module_absent("domain.function_runtime.bridge")
    assert_retired_module_absent("domain.pack_architecture")
