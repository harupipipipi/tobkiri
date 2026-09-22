"""DI Phase 4 contract tests for the canonical Pack v4 container.

The original Phase 4 migration tests treated direct execution helpers as
DI-owned services.  Pack v4 deliberately retired those services: execution is
installed through a captured Broker session, while the host container owns
only canonical support services.
"""
from __future__ import annotations

from pathlib import Path
import threading
import warnings

import pytest

from core_runtime.di_container import get_container

pytestmark = pytest.mark.contract


CANONICAL_SERVICES = (
    "audit_logger",
    "hmac_key_manager",
    "vocab_registry",
    "network_grant_manager",
    "store_registry",
    "approval_manager",
    "capability_trust_store",
    "capability_grant_manager",
    "function_alias_registry",
    "secrets_store",
    "secrets_grant_manager",
    "diagnostics",
    "install_journal",
    "event_bus",
    "health_checker",
    "metrics_collector",
    "profiler",
    "desktop_capability_handler",
    "managed_sandbox_supervisor",
)

RETIRED_DI_SERVICES = (
    "egress_proxy_manager",
    "container_orchestrator",
    "host_privilege_manager",
    "flow_composer",
    "modifier_loader",
    "modifier_applier",
)


# ===================================================================
# register_defaults — canonical service registration
# ===================================================================


class TestRegisterDefaultsPhase4:
    """The v4 host container registers support services only."""

    def test_all_canonical_services_registered(self) -> None:
        container = get_container()
        registered = set(container.registered_names())

        assert set(CANONICAL_SERVICES) <= registered
        assert registered.isdisjoint(RETIRED_DI_SERVICES)
        assert not container.has("permission_manager")

    def test_registered_names_count(self) -> None:
        container = get_container()
        assert len(container.registered_names()) >= len(CANONICAL_SERVICES)


class TestCanonicalServiceResolutionPhase4:
    """Every registered v4 service resolves through the container."""

    @pytest.mark.parametrize("service_name", CANONICAL_SERVICES)
    def test_canonical_service_resolves(
        self,
        service_name: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ManagedSandboxSupervisor imports the pack's canonical top-level
        # ``domain`` namespace, which is available when the pack is launched.
        if service_name == "managed_sandbox_supervisor":
            pack_root = (
                Path(__file__).resolve().parents[1]
                / "ecosystem"
                / "defaultspack"
            )
            monkeypatch.syspath_prepend(str(pack_root))

        container = get_container()
        instance = container.get(service_name)

        assert instance is not None
        assert container.get_or_none(service_name) is instance


# ===================================================================
# Retired direct execution services
# ===================================================================


class TestRetiredServicesPhase4:
    """Retired v3 DI names are absent and cannot be resolved."""

    def test_retired_services_are_absent_and_fail_closed(self) -> None:
        container = get_container()

        for service_name in RETIRED_DI_SERVICES:
            assert not container.has(service_name)
            assert container.get_or_none(service_name) is None
            with pytest.raises(
                KeyError,
                match=rf"Service not registered: {service_name}",
            ):
                container.get(service_name)


# ===================================================================
# FunctionAliasRegistry DI テスト
# ===================================================================


class TestFunctionAliasRegistryDI:

    def test_get_from_container(self) -> None:
        from core_runtime.function_alias import FunctionAliasRegistry

        instance = get_container().get("function_alias_registry")
        assert isinstance(instance, FunctionAliasRegistry)

    def test_get_returns_cached_instance(self) -> None:
        container = get_container()
        assert container.get("function_alias_registry") is container.get(
            "function_alias_registry"
        )

    def test_get_function_alias_registry_returns_di_instance(self) -> None:
        from core_runtime.function_alias import get_function_alias_registry

        di_instance = get_container().get("function_alias_registry")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            func_instance = get_function_alias_registry()
        assert di_instance is func_instance

    def test_deprecation_warning_preserved(self) -> None:
        from core_runtime.function_alias import get_function_alias_registry

        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            get_function_alias_registry()

        assert len(captured) == 1
        assert issubclass(captured[0].category, DeprecationWarning)
        assert "deprecated" in str(captured[0].message).lower()

    def test_reset_produces_new_instance(self) -> None:
        from core_runtime.function_alias import (
            get_function_alias_registry,
            reset_function_alias_registry,
        )

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            old = get_function_alias_registry()
        new = reset_function_alias_registry()

        assert old is not new
        assert isinstance(new, type(old))

    def test_reset_updates_di_cache(self) -> None:
        from core_runtime.function_alias import (
            get_function_alias_registry,
            reset_function_alias_registry,
        )

        reset_function_alias_registry()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            func_instance = get_function_alias_registry()

        assert func_instance is get_container().get("function_alias_registry")

    def test_set_instance_override(self) -> None:
        from core_runtime.function_alias import (
            FunctionAliasRegistry,
            get_function_alias_registry,
        )

        custom = FunctionAliasRegistry()
        get_container().set_instance("function_alias_registry", custom)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert get_function_alias_registry() is custom


# ===================================================================
# SecretsStore DI テスト
# ===================================================================


class TestSecretsStoreDI:

    def test_get_from_container(self) -> None:
        from core_runtime.secrets_store import SecretsStore

        instance = get_container().get("secrets_store")
        assert isinstance(instance, SecretsStore)

    def test_get_returns_cached_instance(self) -> None:
        container = get_container()
        assert container.get("secrets_store") is container.get("secrets_store")

    def test_get_secrets_store_returns_di_instance(self) -> None:
        from core_runtime.secrets_store import get_secrets_store

        di_instance = get_container().get("secrets_store")
        assert di_instance is get_secrets_store()

    def test_reset_produces_new_instance(self, tmp_path) -> None:
        from core_runtime.secrets_store import get_secrets_store, reset_secrets_store

        old = get_secrets_store()
        new = reset_secrets_store(str(tmp_path / "secrets"))

        assert old is not new
        assert isinstance(new, type(old))

    def test_reset_updates_di_cache(self, tmp_path) -> None:
        from core_runtime.secrets_store import get_secrets_store, reset_secrets_store

        reset_secrets_store(str(tmp_path / "secrets"))
        assert get_secrets_store() is get_container().get("secrets_store")

    def test_reset_preserves_signature(self, tmp_path) -> None:
        from core_runtime.secrets_store import reset_secrets_store

        instance = reset_secrets_store(secrets_dir=str(tmp_path / "secrets"))
        assert instance is not None

    def test_set_instance_override(self, tmp_path) -> None:
        from core_runtime.secrets_store import SecretsStore, get_secrets_store

        custom = SecretsStore(str(tmp_path / "custom"))
        get_container().set_instance("secrets_store", custom)
        assert get_secrets_store() is custom


# ===================================================================
# 後方互換テスト for active canonical wrappers
# ===================================================================


class TestBackwardCompatibilityPhase4:

    def test_get_function_alias_registry_signature(self) -> None:
        from core_runtime.function_alias import get_function_alias_registry

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            instance = get_function_alias_registry()
        assert instance is not None

    def test_reset_function_alias_registry_signature(self) -> None:
        from core_runtime.function_alias import (
            FunctionAliasRegistry,
            reset_function_alias_registry,
        )

        assert isinstance(reset_function_alias_registry(), FunctionAliasRegistry)

    def test_get_secrets_store_signature(self) -> None:
        from core_runtime.secrets_store import get_secrets_store

        assert get_secrets_store() is not None

    def test_reset_secrets_store_signature(self, tmp_path) -> None:
        from core_runtime.secrets_store import SecretsStore, reset_secrets_store

        instance = reset_secrets_store(str(tmp_path / "secrets"))
        assert isinstance(instance, SecretsStore)


# ===================================================================
# スレッドセーフテスト
# ===================================================================


class TestThreadSafetyPhase4:

    @staticmethod
    def _concurrent_get(
        service_name: str,
        count: int = 10,
    ) -> tuple[list[object], list[Exception]]:
        results: list[object] = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                results.append(get_container().get(service_name))
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results, errors

    @pytest.mark.parametrize(
        "service_name",
        (
            "function_alias_registry",
            "secrets_store",
            "health_checker",
            "metrics_collector",
            "profiler",
        ),
    )
    def test_concurrent_get_active_canonical_service(self, service_name: str) -> None:
        results, errors = self._concurrent_get(service_name)

        assert errors == []
        assert len(results) == 10
        assert all(instance is results[0] for instance in results)
