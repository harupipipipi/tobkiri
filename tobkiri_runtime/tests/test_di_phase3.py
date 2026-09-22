"""
test_di_phase3.py - DI Phase 3 テスト

対象:
- ApprovalManager の DI コンテナ移行
- PermissionManager の DI コンテナ移行
- register_defaults に 7 サービス全て登録されていること
- 後方互換性（get_xxx / initialize_xxx / reset_xxx が従来通り動作すること）
"""
from __future__ import annotations

import threading

import pytest

from core_runtime.di_container import get_container

pytestmark = pytest.mark.contract


# ===================================================================
# register_defaults — 7 サービス全登録確認
# ===================================================================


class TestRegisterDefaultsPhase3:
    """Phase 3 完了後、register_defaults が 7 サービスを登録していること。"""

    def test_all_seven_services_registered(self) -> None:
        container = get_container()
        expected = [
            "audit_logger",
            "hmac_key_manager",
            "vocab_registry",
            "network_grant_manager",
            "store_registry",
            "approval_manager",
        ]
        for name in expected:
            assert container.has(name), f"Service '{name}' not registered"
        assert not container.has("permission_manager")

    def test_registered_names_count(self) -> None:
        container = get_container()
        names = container.registered_names()
        assert len(names) >= 7


# ===================================================================
# ApprovalManager DI テスト
# ===================================================================


class TestApprovalManagerDI:

    def test_get_from_container(self, tmp_path) -> None:
        """DI コンテナから ApprovalManager を取得できる。"""
        from core_runtime.approval_manager import ApprovalManager
        container = get_container()
        instance = container.get("approval_manager")
        assert isinstance(instance, ApprovalManager)

    def test_get_returns_cached_instance(self) -> None:
        """同一インスタンスがキャッシュされている。"""
        container = get_container()
        first = container.get("approval_manager")
        second = container.get("approval_manager")
        assert first is second

    def test_get_approval_manager_returns_di_instance(self) -> None:
        """get_approval_manager() が DI インスタンスと同一。"""
        from core_runtime.approval_manager import get_approval_manager
        container = get_container()
        di_instance = container.get("approval_manager")
        func_instance = get_approval_manager()
        assert di_instance is func_instance

    def test_initialize_updates_di_cache(self, tmp_path) -> None:
        """initialize_approval_manager() 後に DI コンテナから取得した値が同一。"""
        from core_runtime.approval_manager import (
            get_approval_manager,
            initialize_approval_manager,
        )
        initialized = initialize_approval_manager(
            packs_dir=str(tmp_path / "packs"),
            grants_dir=str(tmp_path / "grants"),
        )
        func_instance = get_approval_manager()
        di_instance = get_container().get("approval_manager")
        assert initialized is func_instance
        assert initialized is di_instance

    def test_reset_produces_new_instance(self, tmp_path) -> None:
        """initialize 後に再度 initialize すると新しいインスタンスが生成される。"""
        from core_runtime.approval_manager import initialize_approval_manager
        old = initialize_approval_manager(
            packs_dir=str(tmp_path / "packs1"),
            grants_dir=str(tmp_path / "grants1"),
        )
        new = initialize_approval_manager(
            packs_dir=str(tmp_path / "packs2"),
            grants_dir=str(tmp_path / "grants2"),
        )
        assert old is not new
        assert isinstance(new, type(old))


# ===================================================================
# PermissionManager DI テスト
# ===================================================================


class TestPermissionManagerDI:
    """The removed PermissionManager is not a v4 DI service."""

    def _assert_removed(self):
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.permission_manager")

    def test_get_from_container(self, tmp_path):
        del tmp_path
        self._assert_removed()

    def test_get_returns_cached_instance(self):
        self._assert_removed()

    def test_get_permission_manager_returns_di_instance(self):
        self._assert_removed()

    def test_reset_produces_new_instance(self):
        self._assert_removed()

    def test_reset_updates_di_cache(self):
        self._assert_removed()


# ===================================================================
# 後方互換テスト
# ===================================================================


class TestBackwardCompatibilityPhase3:

    def test_get_approval_manager_signature(self) -> None:
        """get_approval_manager() が引数なしで呼び出せる。"""
        from core_runtime.approval_manager import get_approval_manager
        instance = get_approval_manager()
        assert instance is not None

    def test_initialize_approval_manager_signature(self, tmp_path) -> None:
        """initialize_approval_manager() が packs_dir, grants_dir 引数を受け取れる。"""
        from core_runtime.approval_manager import initialize_approval_manager
        instance = initialize_approval_manager(
            packs_dir=str(tmp_path / "packs"),
            grants_dir=str(tmp_path / "grants"),
        )
        assert instance is not None

    def test_initialize_approval_manager_default_args(self) -> None:
        """initialize_approval_manager() がデフォルト引数で呼び出せる。"""
        from core_runtime.approval_manager import initialize_approval_manager
        instance = initialize_approval_manager()
        assert instance is not None

    def test_get_permission_manager_signature(self) -> None:
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.permission_manager")

    def test_reset_permission_manager_signature(self) -> None:
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.permission_manager")


# ===================================================================
# スレッドセーフテスト
# ===================================================================


class TestThreadSafetyPhase3:

    def test_concurrent_get_approval_manager(self) -> None:
        """複数スレッドから get_approval_manager() しても同一インスタンス。"""
        from core_runtime.approval_manager import get_approval_manager

        results: list = []
        errors: list = []

        def worker() -> None:
            try:
                results.append(get_approval_manager())
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

    def test_concurrent_get_permission_manager(self) -> None:
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.permission_manager")
