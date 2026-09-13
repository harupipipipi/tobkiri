"""
test_di_phase2.py - DI Phase 2 テスト

対象:
- NetworkGrantManager の DI コンテナ移行
- StoreRegistry の DI コンテナ移行
- register_defaults に 5 サービス全て登録されていること
- 後方互換性（get_xxx / reset_xxx が従来通り動作すること）
"""
from __future__ import annotations

import threading

import pytest

from core_runtime.di_container import get_container, reset_container

pytestmark = pytest.mark.contract


# ===================================================================
# register_defaults — 5 サービス全登録確認
# ===================================================================


class TestRegisterDefaultsPhase2:
    """Phase 2 完了後、register_defaults が 5 サービスを登録していること。"""

    def test_all_five_services_registered(self) -> None:
        container = get_container()
        expected = [
            "audit_logger",
            "hmac_key_manager",
            "vocab_registry",
            "network_grant_manager",
            "store_registry",
        ]
        for name in expected:
            assert container.has(name), f"Service '{name}' not registered"

    def test_registered_names_count(self) -> None:
        container = get_container()
        names = container.registered_names()
        assert len(names) >= 5


# ===================================================================
# NetworkGrantManager DI テスト
# ===================================================================


class TestNetworkGrantManagerDI:

    def test_get_from_container(self, tmp_path) -> None:
        """DI コンテナから NetworkGrantManager を取得できる。"""
        from core_runtime.network_grant_manager import NetworkGrantManager
        container = get_container()
        instance = container.get("network_grant_manager")
        assert isinstance(instance, NetworkGrantManager)

    def test_get_returns_cached_instance(self) -> None:
        """同一インスタンスがキャッシュされている。"""
        container = get_container()
        first = container.get("network_grant_manager")
        second = container.get("network_grant_manager")
        assert first is second

    def test_reset_produces_new_instance(self, tmp_path) -> None:
        """reset 後に新しいインスタンスが生成される。"""
        from core_runtime.network_grant_manager import (
            get_network_grant_manager,
            reset_network_grant_manager,
        )
        old = get_network_grant_manager()
        new = reset_network_grant_manager(str(tmp_path / "grants"))
        assert old is not new
        assert isinstance(new, type(old))

    def test_reset_updates_di_cache(self, tmp_path) -> None:
        """reset 後、DI コンテナのキャッシュも更新されている。"""
        from core_runtime.network_grant_manager import (
            get_network_grant_manager,
            reset_network_grant_manager,
        )
        reset_network_grant_manager(str(tmp_path / "grants"))
        func_instance = get_network_grant_manager()
        di_instance = get_container().get("network_grant_manager")
        assert func_instance is di_instance


class TestRetiredEgressProxyManagerDI:

    def test_egress_proxy_manager_is_absent_and_fail_closed(self) -> None:
        """The retired direct egress service is never a v4 DI entrypoint."""
        reset_container()
        container = get_container()

        assert not container.has("egress_proxy_manager")
        assert container.get_or_none("egress_proxy_manager") is None
        with pytest.raises(
            KeyError,
            match="Service not registered: egress_proxy_manager",
        ):
            container.get("egress_proxy_manager")


# ===================================================================
# StoreRegistry DI テスト
# ===================================================================


class TestStoreRegistryDI:

    def test_get_from_container(self, tmp_path) -> None:
        """DI コンテナから StoreRegistry を取得できる。"""
        from core_runtime.store_registry import StoreRegistry
        container = get_container()
        instance = container.get("store_registry")
        assert isinstance(instance, StoreRegistry)

    def test_get_returns_cached_instance(self) -> None:
        """同一インスタンスがキャッシュされている。"""
        container = get_container()
        first = container.get("store_registry")
        second = container.get("store_registry")
        assert first is second

    def test_reset_produces_new_instance(self, tmp_path) -> None:
        """reset 後に新しいインスタンスが生成される。"""
        from core_runtime.store_registry import (
            get_store_registry,
            reset_store_registry,
        )
        old = get_store_registry()
        new = reset_store_registry(str(tmp_path / "index.json"))
        assert old is not new
        assert isinstance(new, type(old))

    def test_reset_updates_di_cache(self, tmp_path) -> None:
        """reset 後、DI コンテナのキャッシュも更新されている。"""
        from core_runtime.store_registry import (
            get_store_registry,
            reset_store_registry,
        )
        reset_store_registry(str(tmp_path / "index.json"))
        func_instance = get_store_registry()
        di_instance = get_container().get("store_registry")
        assert func_instance is di_instance


# ===================================================================
# 後方互換テスト
# ===================================================================


class TestBackwardCompatibilityPhase2:

    def test_get_network_grant_manager_returns_di_instance(self) -> None:
        """get_network_grant_manager() が DI インスタンスと同一。"""
        from core_runtime.network_grant_manager import get_network_grant_manager
        container = get_container()
        di_instance = container.get("network_grant_manager")
        func_instance = get_network_grant_manager()
        assert di_instance is func_instance

    def test_get_store_registry_returns_di_instance(self) -> None:
        """get_store_registry() が DI インスタンスと同一。"""
        from core_runtime.store_registry import get_store_registry
        container = get_container()
        di_instance = container.get("store_registry")
        func_instance = get_store_registry()
        assert di_instance is func_instance

    def test_reset_network_grant_manager_backward_compat(self, tmp_path) -> None:
        """reset_network_grant_manager() のシグネチャが変わっていない。"""
        from core_runtime.network_grant_manager import reset_network_grant_manager
        # grants_dir=None (default) で呼び出し可能
        instance = reset_network_grant_manager()
        assert instance is not None
        # grants_dir=str で呼び出し可能
        instance2 = reset_network_grant_manager(str(tmp_path / "g"))
        assert instance2 is not None
        assert instance is not instance2

    def test_reset_store_registry_backward_compat(self, tmp_path) -> None:
        """reset_store_registry() のシグネチャが変わっていない。"""
        from core_runtime.store_registry import reset_store_registry
        # index_path=None (default) で呼び出し可能
        instance = reset_store_registry()
        assert instance is not None
        # index_path=str で呼び出し可能
        instance2 = reset_store_registry(str(tmp_path / "idx.json"))
        assert instance2 is not None
        assert instance is not instance2


# ===================================================================
# スレッドセーフテスト
# ===================================================================


class TestThreadSafetyPhase2:

    def test_concurrent_get_network_grant_manager(self) -> None:
        """複数スレッドから get_network_grant_manager() しても同一インスタンス。"""
        from core_runtime.network_grant_manager import get_network_grant_manager

        results: list = []
        errors: list = []

        def worker() -> None:
            try:
                results.append(get_network_grant_manager())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 10
        assert all(r is results[0] for r in results)

    def test_concurrent_get_store_registry(self) -> None:
        """複数スレッドから get_store_registry() しても同一インスタンス。"""
        from core_runtime.store_registry import get_store_registry

        results: list = []
        errors: list = []

        def worker() -> None:
            try:
                results.append(get_store_registry())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 10
        assert all(r is results[0] for r in results)
