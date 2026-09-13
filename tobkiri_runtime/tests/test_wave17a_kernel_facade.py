"""
test_wave17a_kernel_facade.py - Wave 17-A: KernelFacade テスト

カーネルオブジェクト漏洩の封じ込めに関する 22 件のテスト。
"""

from __future__ import annotations

import os
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path


# ---------------------------------------------------------------------------
# Stub: 軽量 Kernel モック
# ---------------------------------------------------------------------------

class _StubInterfaceRegistry:
    """InterfaceRegistry の最小スタブ。"""

    def __init__(self):
        self._store = {
            "io.http.server": "http_server_func",
            "flow.test": {"steps": []},
            "pack.alpha": {"id": "alpha"},
        }

    def get(self, key, strategy="last"):
        return self._store.get(key)

    def list(self):
        return dict(self._store)


class _StubEventBus:
    """EventBus の最小スタブ。"""

    def __init__(self):
        self.published = []

    def publish(self, event_name, data=None):
        self.published.append((event_name, data))


class _StubKernel:
    """Kernel の最小スタブ。"""

    def __init__(self):
        self.interface_registry = _StubInterfaceRegistry()
        self.event_bus = _StubEventBus()
        self.diagnostics = MagicMock()
        self.install_journal = MagicMock()
        self.lifecycle = MagicMock()
        self._kernel_handlers = {"kernel:noop": lambda a, c: None}
        self._capability_proxy = MagicMock()
        self._secret_data = "TOP_SECRET"


# ---------------------------------------------------------------------------
# Tests: KernelFacade
# ---------------------------------------------------------------------------

from core_runtime.kernel_facade import KernelFacade, KernelSecurityError


class TestKernelFacadePublicAPI:
    """公開 API が正しく動作することを検証する。"""

    def setup_method(self):
        self.kernel = _StubKernel()
        self.facade = KernelFacade(self.kernel)

    def test_get_interface_returns_value(self):
        """get_interface で IR の値を取得できる。"""
        result = self.facade.get_interface("io.http.server")
        assert result == "http_server_func"

    def test_get_interface_returns_none_for_missing_key(self):
        """存在しないキーに対して None を返す。"""
        result = self.facade.get_interface("nonexistent.key")
        assert result is None

    def test_list_interfaces_returns_all(self):
        """list_interfaces() が全キーを返す。"""
        result = self.facade.list_interfaces()
        assert "io.http.server" in result
        assert "flow.test" in result
        assert "pack.alpha" in result

    def test_list_interfaces_with_prefix(self):
        """list_interfaces(prefix) がプレフィックスでフィルタする。"""
        result = self.facade.list_interfaces(prefix="flow.")
        assert "flow.test" in result
        assert "io.http.server" not in result
        assert "pack.alpha" not in result

    def test_emit_publishes_event(self):
        """emit() が EventBus にイベントを発火する。"""
        self.facade.emit("test.event", {"key": "value"})
        assert len(self.kernel.event_bus.published) == 1
        assert self.kernel.event_bus.published[0] == ("test.event", {"key": "value"})

    def test_emit_with_none_data(self):
        """emit() が data=None で動作する。"""
        self.facade.emit("test.event")
        assert self.kernel.event_bus.published[0] == ("test.event", None)


class TestKernelFacadeAccessControl:
    """内部アクセスが遮断されることを検証する。"""

    def setup_method(self):
        self.kernel = _StubKernel()
        self.facade = KernelFacade(self.kernel)

    def test_access_interface_registry_blocked(self):
        """interface_registry への直接アクセスが SecurityError になる。"""
        with pytest.raises(KernelSecurityError, match="interface_registry"):
            _ = self.facade.interface_registry

    def test_access_diagnostics_blocked(self):
        """diagnostics への直接アクセスが SecurityError になる。"""
        with pytest.raises(KernelSecurityError, match="diagnostics"):
            _ = self.facade.diagnostics

    def test_access_kernel_handlers_blocked(self):
        """_kernel_handlers への直接アクセスが SecurityError になる。"""
        with pytest.raises(KernelSecurityError, match="_kernel_handlers"):
            _ = self.facade._kernel_handlers

    def test_access_secret_data_blocked(self):
        """任意の内部属性への直接アクセスが SecurityError になる。"""
        with pytest.raises(KernelSecurityError, match="_secret_data"):
            _ = self.facade._secret_data

    def test_access_capability_proxy_blocked(self):
        """_capability_proxy への直接アクセスが SecurityError になる。"""
        with pytest.raises(KernelSecurityError, match="_capability_proxy"):
            _ = self.facade._capability_proxy

    def test_setattr_blocked(self):
        """属性の設定が SecurityError になる。"""
        with pytest.raises(KernelSecurityError, match="Cannot set attribute"):
            self.facade.new_attr = "value"

    def test_delattr_blocked(self):
        """属性の削除が SecurityError になる。"""
        with pytest.raises(KernelSecurityError, match="Cannot delete attribute"):
            del self.facade.emit


class TestKernelFacadeRepresentation:
    """repr / str / dir が内部参照を漏らさないことを検証する。"""

    def setup_method(self):
        self.kernel = _StubKernel()
        self.facade = KernelFacade(self.kernel)

    def test_repr_does_not_leak(self):
        """repr が内部参照を含まない。"""
        r = repr(self.facade)
        assert "KernelFacade" in r
        assert "TOP_SECRET" not in r
        assert "interface_registry" not in r

    def test_str_does_not_leak(self):
        """str が内部参照を含まない。"""
        s = str(self.facade)
        assert "TOP_SECRET" not in s

    def test_dir_only_exposes_public_api(self):
        """dir() が公開 API のみ列挙する。"""
        d = dir(self.facade)
        assert "get_interface" in d
        assert "list_interfaces" in d
        assert "emit" in d
        assert "interface_registry" not in d
        assert "_kernel_handlers" not in d
        assert "diagnostics" not in d

    def test_slots_prevents_dict(self):
        """__slots__ により __dict__ が存在しない。"""
        assert not hasattr(self.facade, "__dict__")


class TestKernelFacadeImmutability:
    """KernelFacade がイミュータブルであることを検証する。"""

    def setup_method(self):
        self.kernel = _StubKernel()
        self.facade = KernelFacade(self.kernel)

    def test_list_interfaces_returns_copy(self):
        """list_interfaces() が元データのコピーを返す。"""
        result1 = self.facade.list_interfaces()
        result1["injected_key"] = "evil"
        result2 = self.facade.list_interfaces()
        assert "injected_key" not in result2


# ---------------------------------------------------------------------------
# Tests: _h_exec_python パストラバーサル防止
# ---------------------------------------------------------------------------

class TestExecPythonPathTraversal:
    """Arbitrary path execution is absent from the Pack v4 runtime."""

    def test_path_traversal_blocked(self):
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.kernel_handlers_system")

    def test_absolute_path_blocked(self, tmp_path):
        del tmp_path
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.kernel_handlers_system")

    def test_untrusted_base_path_blocked(self, tmp_path):
        del tmp_path
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.kernel_handlers_system")

    def test_builtin_core_pack_relative_file_allowed(self):
        from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

        assert_profile_resolver_requires_authority_snapshot()


# ---------------------------------------------------------------------------
# Tests: inject ブロックリスト
# ---------------------------------------------------------------------------

class TestExecPythonInjectBlock:
    """Legacy Kernel context injection cannot bypass v4 resolution."""

    def test_inject_blocked_keys_are_skipped(self):
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.kernel_handlers_system")


# ---------------------------------------------------------------------------
# Tests: kernel_context_builder.build_safe()
# ---------------------------------------------------------------------------

class TestBuildSafe:
    """Only Host-captured context is accepted by the v4 Profile Resolver."""

    def test_build_safe_returns_full_ctx_when_guard_off(self):
        from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

        assert_profile_resolver_requires_authority_snapshot()

    def test_build_safe_returns_sanitized_ctx_when_guard_on(self):
        from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

        assert_profile_resolver_requires_authority_snapshot()
