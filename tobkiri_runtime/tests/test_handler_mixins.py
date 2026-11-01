"""
Handler Mixin ユニットテスト (T-012)

テスト対象:
  - SecretsHandlersMixin   (secrets_handlers.py)
  - ContainerHandlersMixin (container_handlers.py)
  - NetworkHandlersMixin   (network_handlers.py)
  - PackLifecycleHandlersMixin (pack_lifecycle_handlers.py)
  - UnitHandlersMixin      (unit_handlers.py)
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# ダミーモジュール登録 — 各 handler 内の相対 import を解決するため
# __init__.py が全 handler を import するので、テスト対象外 handler が
# 参照するモジュールについてもダミーが必要。
# ---------------------------------------------------------------------------

# audit_logger (共通: _helpers.py が使用)
_dummy_audit = types.ModuleType("tobkiri_runtime.core_runtime.audit_logger")
_dummy_audit_instance = MagicMock()
_dummy_audit.get_audit_logger = MagicMock(return_value=_dummy_audit_instance)
sys.modules.setdefault("tobkiri_runtime.core_runtime.audit_logger", _dummy_audit)

# pack_api_server (flow_handlers / route_handlers が遅延 import)
_dummy_pack_api = types.ModuleType("tobkiri_runtime.core_runtime.pack_api_server")
_dummy_pack_api.APIResponse = MagicMock()
sys.modules.setdefault("tobkiri_runtime.core_runtime.pack_api_server", _dummy_pack_api)

# paths (capability_installer_handlers / pip_handlers / unit_handlers がトップレベル import)
_dummy_paths = types.ModuleType("tobkiri_runtime.core_runtime.paths")
_dummy_paths.is_path_within = MagicMock(return_value=True)
_dummy_paths.BASE_DIR = Path("/tmp")
_dummy_paths.USER_DATA_DIR = Path("/tmp/user_data")
_dummy_paths.CORE_PACK_DIR = Path("/tmp/core")
_dummy_paths.CORE_PACK_ID_PREFIX = "core."
_dummy_paths.discover_pack_locations = MagicMock(return_value=[])
_dummy_paths.ECOSYSTEM_DIR = "/tmp/ecosystem"
sys.modules.setdefault("tobkiri_runtime.core_runtime.paths", _dummy_paths)

# secrets_store
_dummy_secrets_store = types.ModuleType("tobkiri_runtime.core_runtime.secrets_store")
_dummy_secrets_store.get_secrets_store = MagicMock()
sys.modules.setdefault("tobkiri_runtime.core_runtime.secrets_store", _dummy_secrets_store)

# approval_manager
_dummy_approval = types.ModuleType("tobkiri_runtime.core_runtime.approval_manager")


class _PackStatus:
    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"


_dummy_approval.PackStatus = _PackStatus
_dummy_approval.get_approval_manager = MagicMock()
sys.modules.setdefault("tobkiri_runtime.core_runtime.approval_manager", _dummy_approval)

# network_grant_manager
_dummy_ngm = types.ModuleType("tobkiri_runtime.core_runtime.network_grant_manager")
_dummy_ngm.get_network_grant_manager = MagicMock()
sys.modules.setdefault("tobkiri_runtime.core_runtime.network_grant_manager", _dummy_ngm)

# pack_importer / pack_applier
_dummy_pack_importer = types.ModuleType("tobkiri_runtime.core_runtime.pack_importer")
_dummy_pack_importer.get_pack_importer = MagicMock()
sys.modules.setdefault("tobkiri_runtime.core_runtime.pack_importer", _dummy_pack_importer)

_dummy_pack_applier = types.ModuleType("tobkiri_runtime.core_runtime.pack_applier")
_dummy_pack_applier.get_pack_applier = MagicMock()
sys.modules.setdefault("tobkiri_runtime.core_runtime.pack_applier", _dummy_pack_applier)

# store_registry / unit_registry / unit_executor
_dummy_store_registry = types.ModuleType("tobkiri_runtime.core_runtime.store_registry")
_dummy_store_registry.get_store_registry = MagicMock()
sys.modules.setdefault("tobkiri_runtime.core_runtime.store_registry", _dummy_store_registry)

_dummy_unit_registry = types.ModuleType("tobkiri_runtime.core_runtime.unit_registry")
_dummy_unit_registry.get_unit_registry = MagicMock()
sys.modules.setdefault("tobkiri_runtime.core_runtime.unit_registry", _dummy_unit_registry)

_dummy_unit_executor = types.ModuleType("tobkiri_runtime.core_runtime.unit_executor")
_dummy_unit_executor.get_unit_executor = MagicMock()
sys.modules.setdefault("tobkiri_runtime.core_runtime.unit_executor", _dummy_unit_executor)

# capability_grant_manager (capability_grant_handlers が遅延 import)
_dummy_cgm = types.ModuleType("tobkiri_runtime.core_runtime.capability_grant_manager")
_dummy_cgm.get_capability_grant_manager = MagicMock()
sys.modules.setdefault("tobkiri_runtime.core_runtime.capability_grant_manager", _dummy_cgm)

# store_sharing_manager (store_share_handlers が遅延 import)
_dummy_ssm = types.ModuleType("tobkiri_runtime.core_runtime.store_sharing_manager")
_dummy_ssm.get_shared_store_manager = MagicMock()
sys.modules.setdefault("tobkiri_runtime.core_runtime.store_sharing_manager", _dummy_ssm)

# capability_installer (capability_installer_handlers が遅延 import)
_dummy_ci = types.ModuleType("tobkiri_runtime.core_runtime.capability_installer")
_dummy_ci.get_capability_installer = MagicMock()
sys.modules.setdefault("tobkiri_runtime.core_runtime.capability_installer", _dummy_ci)

# pip_installer (pip_handlers が遅延 import)
_dummy_pip = types.ModuleType("tobkiri_runtime.core_runtime.pip_installer")
_dummy_pip.get_pip_installer = MagicMock()
sys.modules.setdefault("tobkiri_runtime.core_runtime.pip_installer", _dummy_pip)

# ---------------------------------------------------------------------------
# handler mixin インポート
# ---------------------------------------------------------------------------
from tobkiri_runtime.core_runtime.api.secrets_handlers import (  # noqa: E402
    SecretsHandlersMixin,
)
from tobkiri_runtime.core_runtime.api.container_handlers import (  # noqa: E402
    ContainerHandlersMixin,
)
from tobkiri_runtime.core_runtime.api.network_handlers import (  # noqa: E402
    NetworkHandlersMixin,
)
from tobkiri_runtime.core_runtime.api.pack_lifecycle_handlers import (  # noqa: E402
    PackLifecycleHandlersMixin,
)
from tobkiri_runtime.core_runtime.api.unit_handlers import (  # noqa: E402
    UnitHandlersMixin,
)
from tobkiri_runtime.core_runtime.api._helpers import _SAFE_ERROR_MSG  # noqa: E402


# ======================================================================
# TestSecretsHandlers
# ======================================================================

class _SecretsStub(SecretsHandlersMixin):
    """SecretsHandlersMixin を利用可能にするための最小スタブ"""
    pass


class TestSecretsHandlers:
    """SecretsHandlersMixin のテスト"""

    def _make(self) -> _SecretsStub:
        return _SecretsStub()

    # --- _secrets_list ---

    def test_secrets_list_success(self):
        handler = self._make()
        mock_key = MagicMock()
        mock_key.to_dict.return_value = {"key": "API_KEY", "created_at": "2025-01-01"}
        mock_store = MagicMock()
        mock_store.list_keys.return_value = [mock_key]
        with patch(
            "tobkiri_runtime.core_runtime.secrets_store.get_secrets_store",
            return_value=mock_store,
        ):
            result = handler._secrets_list()
        assert result["count"] == 1
        assert result["keys"] == [{"key": "API_KEY", "created_at": "2025-01-01"}]

    def test_secrets_list_import_error(self):
        handler = self._make()
        with patch(
            "tobkiri_runtime.core_runtime.secrets_store.get_secrets_store",
            side_effect=RuntimeError("module broken"),
        ):
            result = handler._secrets_list()
        assert result["keys"] == []
        assert "error" in result

    # --- _secrets_set ---

    def test_secrets_set_success(self):
        handler = self._make()
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {"success": True, "key": "MY_KEY"}
        mock_store = MagicMock()
        mock_store.set_secret.return_value = mock_result
        with patch(
            "tobkiri_runtime.core_runtime.secrets_store.get_secrets_store",
            return_value=mock_store,
        ):
            result = handler._secrets_set({"key": "MY_KEY", "value": "secret123"})
        assert result["success"] is True

    def test_secrets_set_missing_key(self):
        handler = self._make()
        result = handler._secrets_set({"value": "secret123"})
        assert result["success"] is False
        assert "key" in result["error"].lower()

    def test_secrets_set_invalid_key_pattern(self):
        handler = self._make()
        result = handler._secrets_set({"key": "invalid-key!", "value": "v"})
        assert result["success"] is False
        assert "Invalid key" in result["error"]

    def test_secrets_set_key_lowercase_rejected(self):
        handler = self._make()
        result = handler._secrets_set({"key": "lower_case", "value": "v"})
        assert result["success"] is False
        assert "Invalid key" in result["error"]

    def test_secrets_set_value_too_large(self):
        handler = self._make()
        big_value = "x" * (1_048_576 + 1)
        result = handler._secrets_set({"key": "BIG_KEY", "value": big_value})
        assert result["success"] is False
        assert "too large" in result["error"].lower()

    def test_secrets_set_value_not_string(self):
        handler = self._make()
        result = handler._secrets_set({"key": "MY_KEY", "value": 12345})
        assert result["success"] is False
        assert "string" in result["error"].lower()

    # --- _secrets_delete ---

    def test_secrets_delete_success(self):
        handler = self._make()
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {"success": True, "key": "DEL_KEY"}
        mock_store = MagicMock()
        mock_store.delete_secret.return_value = mock_result
        with patch(
            "tobkiri_runtime.core_runtime.secrets_store.get_secrets_store",
            return_value=mock_store,
        ):
            result = handler._secrets_delete({"key": "DEL_KEY"})
        assert result["success"] is True

    def test_secrets_delete_missing_key(self):
        handler = self._make()
        result = handler._secrets_delete({})
        assert result["success"] is False
        assert "key" in result["error"].lower()

    def test_secrets_delete_invalid_key(self):
        handler = self._make()
        result = handler._secrets_delete({"key": "bad key spaces"})
        assert result["success"] is False
        assert "Invalid key" in result["error"]


# ======================================================================
# TestContainerHandlers
# ======================================================================

class _ContainerStub(ContainerHandlersMixin):
    """ContainerHandlersMixin を利用可能にするための最小スタブ"""

    def __init__(self, orchestrator=None, approval_mgr=None):
        self.container_orchestrator = orchestrator
        self.approval_manager = approval_mgr


class TestContainerHandlers:
    """ContainerHandlersMixin のテスト"""

    # --- _get_containers ---

    def test_get_containers_success(self):
        orch = MagicMock()
        orch.list_containers.return_value = [{"id": "c1"}, {"id": "c2"}]
        handler = _ContainerStub(orchestrator=orch)
        result = handler._get_containers()
        assert len(result) == 2
        assert result[0]["id"] == "c1"

    def test_get_containers_no_orchestrator(self):
        handler = _ContainerStub(orchestrator=None)
        result = handler._get_containers()
        assert result == []

    # --- _start_container ---

    def test_start_container_success(self):
        orch = MagicMock()
        orch.start_container.return_value = MagicMock(
            success=True, container_id="abc123", error=None,
        )
        approval = MagicMock()
        approval.get_status.return_value = _PackStatus.APPROVED
        handler = _ContainerStub(orchestrator=orch, approval_mgr=approval)
        result = handler._start_container("pack1")
        assert result["success"] is True
        assert result["container_id"] == "abc123"

    def test_start_container_no_orchestrator(self):
        handler = _ContainerStub(orchestrator=None)
        result = handler._start_container("pack1")
        assert result["success"] is False
        assert "not initialized" in result["error"].lower()

    def test_start_container_not_approved(self):
        orch = MagicMock()
        approval = MagicMock()
        approval.get_status.return_value = _PackStatus.PENDING
        handler = _ContainerStub(orchestrator=orch, approval_mgr=approval)
        result = handler._start_container("pack1")
        assert result["success"] is False
        assert result.get("status_code") == 403

    def test_start_container_no_approval_manager_skips_check(self):
        orch = MagicMock()
        orch.start_container.return_value = MagicMock(
            success=True, container_id="c1", error=None,
        )
        handler = _ContainerStub(orchestrator=orch, approval_mgr=None)
        result = handler._start_container("pack1")
        assert result["success"] is True

    # --- _stop_container ---

    def test_stop_container_success(self):
        orch = MagicMock()
        orch.stop_container.return_value = MagicMock(success=True)
        handler = _ContainerStub(orchestrator=orch)
        result = handler._stop_container("pack1")
        assert result["success"] is True

    def test_stop_container_no_orchestrator(self):
        handler = _ContainerStub(orchestrator=None)
        result = handler._stop_container("pack1")
        assert result["success"] is False

    # --- _remove_container ---

    def test_remove_container_success(self):
        orch = MagicMock()
        handler = _ContainerStub(orchestrator=orch)
        result = handler._remove_container("pack1")
        assert result["success"] is True
        assert result["pack_id"] == "pack1"
        orch.stop_container.assert_called_once_with("pack1")
        orch.remove_container.assert_called_once_with("pack1")

    def test_remove_container_no_orchestrator(self):
        handler = _ContainerStub(orchestrator=None)
        result = handler._remove_container("pack1")
        assert result["success"] is False

    # --- _get_docker_status ---

    def test_get_docker_status_available_via_orchestrator(self):
        orch = MagicMock()
        orch.is_docker_available.return_value = True
        handler = _ContainerStub(orchestrator=orch)
        result = handler._get_docker_status()
        assert result["available"] is True
        assert result["required"] is True

    def test_get_docker_status_unavailable_via_orchestrator(self):
        orch = MagicMock()
        orch.is_docker_available.return_value = False
        handler = _ContainerStub(orchestrator=orch)
        result = handler._get_docker_status()
        assert result["available"] is False

    def test_get_docker_status_fallback_subprocess_success(self):
        handler = _ContainerStub(orchestrator=None)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = handler._get_docker_status()
        assert result["available"] is True

    def test_get_docker_status_fallback_subprocess_failure(self):
        handler = _ContainerStub(orchestrator=None)
        with patch(
            "subprocess.run",
            side_effect=FileNotFoundError("docker not found"),
        ):
            result = handler._get_docker_status()
        assert result["available"] is False


# ======================================================================
# TestNetworkHandlers
# ======================================================================

class _NetworkStub(NetworkHandlersMixin):
    """NetworkHandlersMixin を利用可能にするための最小スタブ"""
    pass


class TestNetworkHandlers:
    """NetworkHandlersMixin のテスト"""

    _PATCH_NGM = (
        "tobkiri_runtime.core_runtime.network_grant_manager"
        ".get_network_grant_manager"
    )

    # --- _network_grant ---

    def test_network_grant_success(self):
        handler = _NetworkStub()
        mock_grant = MagicMock()
        mock_grant.to_dict.return_value = {
            "pack_id": "p1",
            "allowed_domains": ["example.com"],
            "allowed_ports": [443],
        }
        mock_ngm = MagicMock()
        mock_ngm.grant_network_access.return_value = mock_grant
        with patch(self._PATCH_NGM, return_value=mock_ngm):
            result = handler._network_grant(
                pack_id="p1",
                allowed_domains=["example.com"],
                allowed_ports=[443],
            )
        assert result["success"] is True
        assert result["pack_id"] == "p1"
        assert "grant" in result

    def test_network_grant_exception(self):
        handler = _NetworkStub()
        with patch(self._PATCH_NGM, side_effect=RuntimeError("boom")):
            result = handler._network_grant("p1", [], [])
        assert result["success"] is False
        assert result["error"] == _SAFE_ERROR_MSG

    # --- _network_revoke ---

    def test_network_revoke_success(self):
        handler = _NetworkStub()
        mock_ngm = MagicMock()
        mock_ngm.revoke_network_access.return_value = True
        with patch(self._PATCH_NGM, return_value=mock_ngm):
            result = handler._network_revoke("p1", reason="test revoke")
        assert result["success"] is True
        assert result["revoked"] is True
        assert result["pack_id"] == "p1"

    def test_network_revoke_exception(self):
        handler = _NetworkStub()
        with patch(self._PATCH_NGM, side_effect=RuntimeError("fail")):
            result = handler._network_revoke("p1")
        assert result["success"] is False
        assert result["error"] == _SAFE_ERROR_MSG

    # --- _network_check ---

    def test_network_check_allowed(self):
        handler = _NetworkStub()
        mock_result = MagicMock(
            allowed=True, reason="granted",
            pack_id="p1", domain="example.com", port=443,
        )
        mock_ngm = MagicMock()
        mock_ngm.check_access.return_value = mock_result
        with patch(self._PATCH_NGM, return_value=mock_ngm):
            result = handler._network_check("p1", "example.com", 443)
        assert result["allowed"] is True
        assert result["domain"] == "example.com"
        assert result["port"] == 443

    def test_network_check_denied(self):
        handler = _NetworkStub()
        mock_result = MagicMock(
            allowed=False, reason="no grant",
            pack_id="p1", domain="evil.com", port=80,
        )
        mock_ngm = MagicMock()
        mock_ngm.check_access.return_value = mock_result
        with patch(self._PATCH_NGM, return_value=mock_ngm):
            result = handler._network_check("p1", "evil.com", 80)
        assert result["allowed"] is False
        assert result["reason"] == "no grant"

    def test_network_check_exception(self):
        handler = _NetworkStub()
        with patch(self._PATCH_NGM, side_effect=RuntimeError("fail")):
            result = handler._network_check("p1", "x.com", 80)
        assert result["allowed"] is False
        assert "error" in result

    # --- _network_list ---

    def test_network_list_success(self):
        handler = _NetworkStub()
        mock_grant_obj = MagicMock()
        mock_grant_obj.to_dict.return_value = {"allowed_domains": ["a.com"]}
        mock_ngm = MagicMock()
        mock_ngm.get_all_grants.return_value = {"p1": mock_grant_obj}
        mock_ngm.get_disabled_packs.return_value = {"p2"}
        with patch(self._PATCH_NGM, return_value=mock_ngm):
            result = handler._network_list()
        assert result["grant_count"] == 1
        assert result["disabled_count"] == 1
        assert "p1" in result["grants"]
        assert "p2" in result["disabled_packs"]

    def test_network_list_exception(self):
        handler = _NetworkStub()
        with patch(self._PATCH_NGM, side_effect=RuntimeError("fail")):
            result = handler._network_list()
        assert result["grants"] == {}
        assert "error" in result


# ======================================================================
# TestPackLifecycleHandlers
# ======================================================================

class _LifecycleStub(PackLifecycleHandlersMixin):
    """PackLifecycleHandlersMixin を利用可能にするための最小スタブ"""

    def __init__(self, orchestrator=None, approval_mgr=None, privilege_mgr=None):
        self.container_orchestrator = orchestrator
        self.approval_manager = approval_mgr
        self.host_privilege_manager = privilege_mgr


class TestPackLifecycleHandlers:
    """PackLifecycleHandlersMixin のテスト"""

    # --- _uninstall_pack ---

    def test_uninstall_pack_all_success(self):
        orch = MagicMock()
        approval = MagicMock()
        priv = MagicMock()
        handler = _LifecycleStub(
            orchestrator=orch, approval_mgr=approval, privilege_mgr=priv,
        )
        result = handler._uninstall_pack("pack1")
        assert result["success"] is True
        assert result["pack_id"] == "pack1"
        assert result["steps"]["container_stop"] is True
        assert result["steps"]["container_remove"] is True
        assert result["steps"]["approval_remove"] is True
        assert result["steps"]["privilege_revoke"] is True
        assert result["errors"] == []
        orch.stop_container.assert_called_once_with("pack1")
        orch.remove_container.assert_called_once_with("pack1")
        approval.remove_approval.assert_called_once_with("pack1")
        priv.revoke_all.assert_called_once_with("pack1")

    def test_uninstall_pack_partial_failure_container_stop(self):
        orch = MagicMock()
        orch.stop_container.side_effect = RuntimeError("stop failed")
        orch.remove_container.return_value = None
        approval = MagicMock()
        priv = MagicMock()
        handler = _LifecycleStub(
            orchestrator=orch, approval_mgr=approval, privilege_mgr=priv,
        )
        result = handler._uninstall_pack("pack1")
        assert result["success"] is False
        assert result["steps"]["container_stop"] is False
        assert result["steps"]["container_remove"] is True
        assert result["steps"]["approval_remove"] is True
        assert result["steps"]["privilege_revoke"] is True
        assert len(result["errors"]) == 1
        assert result["errors"][0]["step"] == "container_stop"

    def test_uninstall_pack_multiple_failures(self):
        orch = MagicMock()
        orch.stop_container.side_effect = RuntimeError("stop failed")
        orch.remove_container.side_effect = RuntimeError("remove failed")
        approval = MagicMock()
        approval.remove_approval.side_effect = RuntimeError("approval failed")
        priv = MagicMock()
        handler = _LifecycleStub(
            orchestrator=orch, approval_mgr=approval, privilege_mgr=priv,
        )
        result = handler._uninstall_pack("pack1")
        assert result["success"] is False
        assert len(result["errors"]) == 3
        failed_steps = {e["step"] for e in result["errors"]}
        assert failed_steps == {
            "container_stop", "container_remove", "approval_remove",
        }

    def test_uninstall_pack_empty_pack_id(self):
        handler = _LifecycleStub()
        result = handler._uninstall_pack("")
        assert result["success"] is False
        assert result["errors"][0]["step"] == "validation"

    def test_uninstall_pack_whitespace_only_pack_id(self):
        handler = _LifecycleStub()
        result = handler._uninstall_pack("   ")
        assert result["success"] is False
        assert result["errors"][0]["step"] == "validation"

    def test_uninstall_pack_non_string_pack_id(self):
        handler = _LifecycleStub()
        result = handler._uninstall_pack(12345)
        assert result["success"] is False
        assert result["errors"][0]["step"] == "validation"

    def test_uninstall_pack_no_managers_all_skipped(self):
        handler = _LifecycleStub(
            orchestrator=None, approval_mgr=None, privilege_mgr=None,
        )
        result = handler._uninstall_pack("pack1")
        assert result["success"] is True
        assert result["steps"]["container_stop"] is None
        assert result["steps"]["container_remove"] is None
        assert result["steps"]["approval_remove"] is None
        assert result["steps"]["privilege_revoke"] is None
        assert result["errors"] == []

    # --- _pack_import ---

    def test_pack_import_success(self):
        handler = _LifecycleStub()
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {"success": True, "staging_id": "s1"}
        mock_importer = MagicMock()
        mock_importer.import_pack.return_value = mock_result
        with patch(
            "tobkiri_runtime.core_runtime.pack_importer.get_pack_importer",
            return_value=mock_importer,
        ):
            result = handler._pack_import("/tmp/pack.zip", notes="test")
        assert result["success"] is True
        assert result["staging_id"] == "s1"
        mock_importer.import_pack.assert_called_once_with(
            "/tmp/pack.zip", notes="test",
        )

    def test_pack_import_exception(self):
        handler = _LifecycleStub()
        with patch(
            "tobkiri_runtime.core_runtime.pack_importer.get_pack_importer",
            side_effect=RuntimeError("import boom"),
        ):
            result = handler._pack_import("/bad/path")
        assert result["success"] is False
        assert result["error"] == _SAFE_ERROR_MSG

    # --- _pack_apply ---

    def test_pack_apply_is_retired_and_denies_unsigned_apply(self):
        handler = _LifecycleStub()
        mock_importer = MagicMock()
        mock_applier = MagicMock()
        with patch(
            "tobkiri_runtime.core_runtime.pack_importer.get_pack_importer",
            return_value=mock_importer,
        ), patch(
            "tobkiri_runtime.core_runtime.pack_applier.get_pack_applier",
            return_value=mock_applier,
        ):
            result = handler._pack_apply("s1", mode="replace")
        assert result["success"] is False
        assert result["denied"] is True
        assert "signed" in result["error"].lower()
        mock_importer.get_staging_meta.assert_not_called()
        mock_applier.apply.assert_not_called()

    def test_pack_apply_denies_regardless_of_authenticated_actor(self):
        handler = _LifecycleStub()
        handler._authenticated_principal = types.SimpleNamespace(
            principal_id="profile:work__surface:mobile",
        )
        result = handler._pack_apply("s1", mode="replace")
        assert result["success"] is False
        assert result["denied"] is True

    def test_pack_apply_denies_unknown_staging(self):
        handler = _LifecycleStub()
        result = handler._pack_apply("nonexistent")
        assert result["success"] is False
        assert result["denied"] is True

    def test_pack_apply_denies_instead_of_touching_importer(self):
        handler = _LifecycleStub()
        with patch(
            "tobkiri_runtime.core_runtime.pack_importer.get_pack_importer",
            side_effect=RuntimeError("apply boom"),
        ):
            result = handler._pack_apply("s1")
        assert result["success"] is False
        assert result["denied"] is True


# ======================================================================
# TestUnitHandlers
# ======================================================================

class _UnitStub(UnitHandlersMixin):
    """UnitHandlersMixin を利用可能にするための最小スタブ"""
    pass


class TestUnitHandlers:
    """UnitHandlersMixin のテスト"""

    _PATCH_STORE_REG = (
        "tobkiri_runtime.core_runtime.store_registry.get_store_registry"
    )
    _PATCH_UNIT_REG = (
        "tobkiri_runtime.core_runtime.unit_registry.get_unit_registry"
    )
    _PATCH_EXECUTOR = (
        "tobkiri_runtime.core_runtime.unit_executor.get_unit_executor"
    )
    _PATCH_PATH_WITHIN = "tobkiri_runtime.core_runtime.paths.is_path_within"

    # --- _units_list ---

    def test_units_list_all_stores(self):
        handler = _UnitStub()
        mock_unit = MagicMock()
        mock_unit.to_dict.return_value = {"name": "u1", "namespace": "ns"}
        mock_store_reg = MagicMock()
        mock_store_reg.list_stores.return_value = [
            {"store_id": "s1", "root_path": "/tmp/store1"},
        ]
        mock_unit_reg = MagicMock()
        mock_unit_reg.list_units.return_value = [mock_unit]
        with patch(self._PATCH_STORE_REG, return_value=mock_store_reg), \
             patch(self._PATCH_UNIT_REG, return_value=mock_unit_reg):
            result = handler._units_list()
        assert result["count"] == 1
        assert len(result["units"]) == 1
        assert result["units"][0]["name"] == "u1"

    def test_units_list_multiple_stores(self):
        handler = _UnitStub()
        mock_unit_a = MagicMock()
        mock_unit_a.to_dict.return_value = {"name": "ua"}
        mock_unit_b = MagicMock()
        mock_unit_b.to_dict.return_value = {"name": "ub"}
        mock_store_reg = MagicMock()
        mock_store_reg.list_stores.return_value = [
            {"store_id": "s1", "root_path": "/tmp/store1"},
            {"store_id": "s2", "root_path": "/tmp/store2"},
        ]
        mock_unit_reg = MagicMock()
        mock_unit_reg.list_units.side_effect = [
            [mock_unit_a], [mock_unit_b],
        ]
        with patch(self._PATCH_STORE_REG, return_value=mock_store_reg), \
             patch(self._PATCH_UNIT_REG, return_value=mock_unit_reg):
            result = handler._units_list()
        assert result["count"] == 2

    def test_units_list_specific_store(self):
        handler = _UnitStub()
        mock_unit = MagicMock()
        mock_unit.to_dict.return_value = {"name": "u1"}
        mock_store_def = MagicMock()
        mock_store_def.root_path = "/tmp/store1"
        mock_store_reg = MagicMock()
        mock_store_reg.get_store.return_value = mock_store_def
        mock_unit_reg = MagicMock()
        mock_unit_reg.list_units.return_value = [mock_unit]
        with patch(self._PATCH_STORE_REG, return_value=mock_store_reg), \
             patch(self._PATCH_UNIT_REG, return_value=mock_unit_reg):
            result = handler._units_list(store_id="s1")
        assert result["count"] == 1
        assert result["store_id"] == "s1"

    def test_units_list_store_not_found(self):
        handler = _UnitStub()
        mock_store_reg = MagicMock()
        mock_store_reg.get_store.return_value = None
        with patch(self._PATCH_STORE_REG, return_value=mock_store_reg), \
             patch(self._PATCH_UNIT_REG, return_value=MagicMock()):
            result = handler._units_list(store_id="nonexistent")
        assert result["units"] == []
        assert "not found" in result["error"].lower()

    def test_units_list_exception(self):
        handler = _UnitStub()
        with patch(
            self._PATCH_STORE_REG, side_effect=RuntimeError("fail"),
        ):
            result = handler._units_list()
        assert result["units"] == []
        assert "error" in result

    # --- _units_publish ---

    def _publish_body(self, **overrides) -> dict:
        base = {
            "store_id": "s1",
            "source_dir": "/tmp/ecosystem/src",
            "namespace": "ns",
            "name": "u1",
            "version": "1.0",
        }
        base.update(overrides)
        return base

    def test_units_publish_success(self):
        handler = _UnitStub()
        mock_store_def = MagicMock()
        mock_store_def.root_path = "/tmp/store1"
        mock_store_reg = MagicMock()
        mock_store_reg.get_store.return_value = mock_store_def
        mock_pub_result = MagicMock()
        mock_pub_result.to_dict.return_value = {
            "success": True, "unit_id": "ns/u1/1.0",
        }
        mock_unit_reg = MagicMock()
        mock_unit_reg.publish_unit.return_value = mock_pub_result
        with patch(self._PATCH_STORE_REG, return_value=mock_store_reg), \
             patch(self._PATCH_UNIT_REG, return_value=mock_unit_reg), \
             patch(self._PATCH_PATH_WITHIN, return_value=True):
            result = handler._units_publish(self._publish_body())
        assert result["success"] is True
        assert result["unit_id"] == "ns/u1/1.0"

    def test_units_publish_missing_store_id(self):
        handler = _UnitStub()
        result = handler._units_publish(self._publish_body(store_id=""))
        assert result["success"] is False
        assert "missing" in result["error"].lower()

    def test_units_publish_missing_source_dir(self):
        handler = _UnitStub()
        result = handler._units_publish(self._publish_body(source_dir=""))
        assert result["success"] is False
        assert "missing" in result["error"].lower()

    def test_units_publish_missing_namespace(self):
        handler = _UnitStub()
        result = handler._units_publish(self._publish_body(namespace=""))
        assert result["success"] is False

    def test_units_publish_missing_name(self):
        handler = _UnitStub()
        result = handler._units_publish(self._publish_body(name=""))
        assert result["success"] is False

    def test_units_publish_missing_version(self):
        handler = _UnitStub()
        result = handler._units_publish(self._publish_body(version=""))
        assert result["success"] is False

    def test_units_publish_path_traversal_rejected(self):
        handler = _UnitStub()
        with patch(self._PATCH_PATH_WITHIN, return_value=False), \
             patch(self._PATCH_STORE_REG, return_value=MagicMock()), \
             patch(self._PATCH_UNIT_REG, return_value=MagicMock()):
            result = handler._units_publish(
                self._publish_body(source_dir="/etc/passwd"),
            )
        assert result["success"] is False
        assert "outside" in result["error"].lower()

    def test_units_publish_store_not_found(self):
        handler = _UnitStub()
        mock_store_reg = MagicMock()
        mock_store_reg.get_store.return_value = None
        with patch(self._PATCH_STORE_REG, return_value=mock_store_reg), \
             patch(self._PATCH_UNIT_REG, return_value=MagicMock()), \
             patch(self._PATCH_PATH_WITHIN, return_value=True):
            result = handler._units_publish(
                self._publish_body(store_id="nope"),
            )
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    # --- _units_execute ---

    def _exec_body(self, **overrides) -> dict:
        base = {
            "principal_id": "user1",
            "unit_ref": {"namespace": "ns", "name": "u1"},
            "args": {"x": 1},
            "timeout": 30,
        }
        base.update(overrides)
        return base

    def _mock_executor(self, success=True, error_type=None, extra=None):
        mock_result = MagicMock()
        mock_result.success = success
        mock_result.error_type = error_type
        rd = {"success": success}
        if extra:
            rd.update(extra)
        mock_result.to_dict.return_value = rd
        mock_exec = MagicMock()
        mock_exec.execute.return_value = mock_result
        return mock_exec

    def test_units_execute_success(self):
        handler = _UnitStub()
        mock_exec = self._mock_executor(
            success=True, extra={"output": "ok"},
        )
        with patch(self._PATCH_EXECUTOR, return_value=mock_exec):
            result = handler._units_execute(self._exec_body())
        assert result["success"] is True

    def test_units_execute_missing_principal_id(self):
        handler = _UnitStub()
        result = handler._units_execute(
            self._exec_body(principal_id=""),
        )
        assert result["success"] is False
        assert "principal_id" in result["error"]

    def test_units_execute_missing_unit_ref(self):
        handler = _UnitStub()
        body = self._exec_body()
        del body["unit_ref"]
        result = handler._units_execute(body)
        assert result["success"] is False
        assert "unit_ref" in result["error"]

    def test_units_execute_invalid_unit_ref_type(self):
        handler = _UnitStub()
        result = handler._units_execute(
            self._exec_body(unit_ref="not_a_dict"),
        )
        assert result["success"] is False
        assert "unit_ref" in result["error"]

    def test_units_execute_timeout_non_numeric_defaults_to_60(self):
        handler = _UnitStub()
        mock_exec = self._mock_executor()
        with patch(self._PATCH_EXECUTOR, return_value=mock_exec):
            handler._units_execute(self._exec_body(timeout="fast"))
        call_kw = mock_exec.execute.call_args
        assert call_kw.kwargs.get("timeout_seconds") == 60.0

    def test_units_execute_timeout_clamped_to_minimum_1(self):
        handler = _UnitStub()
        mock_exec = self._mock_executor()
        with patch(self._PATCH_EXECUTOR, return_value=mock_exec):
            handler._units_execute(self._exec_body(timeout=0.1))
        call_kw = mock_exec.execute.call_args
        assert call_kw.kwargs.get("timeout_seconds") == 1

    def test_units_execute_timeout_clamped_to_maximum_300(self):
        handler = _UnitStub()
        mock_exec = self._mock_executor()
        with patch(self._PATCH_EXECUTOR, return_value=mock_exec):
            handler._units_execute(self._exec_body(timeout=9999))
        call_kw = mock_exec.execute.call_args
        assert call_kw.kwargs.get("timeout_seconds") == 300

    def test_units_execute_error_type_included_on_failure(self):
        handler = _UnitStub()
        mock_exec = self._mock_executor(
            success=False, error_type="PermissionDenied",
            extra={"error": "denied"},
        )
        with patch(self._PATCH_EXECUTOR, return_value=mock_exec):
            result = handler._units_execute(self._exec_body())
        assert result["success"] is False
        assert result["error_type"] == "PermissionDenied"

    def test_units_execute_exception_returns_safe_error(self):
        handler = _UnitStub()
        with patch(
            self._PATCH_EXECUTOR,
            side_effect=RuntimeError("executor boom"),
        ):
            result = handler._units_execute(self._exec_body())
        assert result["success"] is False
        assert result["error"] == _SAFE_ERROR_MSG
