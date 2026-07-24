"""ControlPanelHandlersMixin の基本テスト"""
from __future__ import annotations

import json
import sys
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# テスト対象のインポートパスを解決
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from core_runtime.api.control_panel_handlers import ControlPanelHandlersMixin
from core_runtime.app_version import APP_DISPLAY_VERSION
import core_runtime.api.control_panel_handlers as control_panel_handlers
from core_runtime.github_update_manager import GitHubUpdateError


class _FakeHandler(ControlPanelHandlersMixin):
    """テスト用のフェイクハンドラ（Mixin を単体テストするため）"""
    kernel = None
    app_lifecycle_manager = None


class TestPanelGetDashboard(unittest.TestCase):
    """GET /api/panel/dashboard のレスポンス形式テスト"""

    def test_dashboard_returns_required_keys(self):
        handler = _FakeHandler()
        # kernel なしでも動作する（数値は 0）
        result = handler._panel_get_dashboard()
        self.assertIn("packs", result)
        self.assertIn("flows", result)
        self.assertIn("kernel", result)
        self.assertIn("profile", result)

    def test_dashboard_packs_structure(self):
        handler = _FakeHandler()
        result = handler._panel_get_dashboard()
        packs = result["packs"]
        self.assertIn("total", packs)
        self.assertIn("enabled", packs)
        self.assertIn("disabled", packs)
        self.assertIsInstance(packs["total"], int)

    def test_dashboard_flows_structure(self):
        handler = _FakeHandler()
        result = handler._panel_get_dashboard()
        flows = result["flows"]
        self.assertIn("total", flows)
        self.assertIsInstance(flows["total"], int)


class TestPanelGetPacks(unittest.TestCase):
    """GET /api/panel/packs のレスポンス形式テスト"""

    def test_packs_returns_list_and_count(self):
        handler = _FakeHandler()
        result = handler._panel_get_packs()
        self.assertIn("packs", result)
        self.assertIn("count", result)
        self.assertIsInstance(result["packs"], list)
        self.assertEqual(result["count"], len(result["packs"]))

    @patch.object(ControlPanelHandlersMixin, "_panel_read_pack_overrides")
    @patch("core_runtime.paths.discover_pack_locations", create=True)
    def test_packs_include_approval_state(self, mock_discover, mock_read_overrides):
        class FakeApprovalManager:
            def get_status(self, pack_id):
                return SimpleNamespace(value="modified")

            def is_pack_approved_and_verified(self, pack_id):
                return False, "hash_mismatch"

            def verify_hash_detailed(self, pack_id, use_cache=True):
                return {"valid": False, "critical_changed": True}

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            eco_path = root / "ecosystem.json"
            eco_path.write_text(json.dumps({
                "pack_id": "pack_a",
                "enabled": True,
                "metadata": {"name": "Pack A"},
            }), encoding="utf-8")
            mock_read_overrides.return_value = {}
            mock_discover.return_value = [
                SimpleNamespace(pack_id="pack_a", ecosystem_json_path=eco_path)
            ]

            handler = _FakeHandler()
            handler.approval_manager = FakeApprovalManager()
            result = handler._panel_get_packs()

            pack = next(p for p in result["packs"] if p["pack_id"] == "pack_a")
            self.assertEqual(pack["approval_status"], "modified")
            self.assertFalse(pack["approved"])
            self.assertEqual(pack["approval_reason"], "hash_mismatch")
            self.assertFalse(pack["hash_valid"])
            self.assertTrue(pack["critical_changed"])
            self.assertIn("hash_mismatch", pack["approval_issues"])
            self.assertIn("critical_changed", pack["approval_issues"])

    @patch.object(ControlPanelHandlersMixin, "_panel_read_pack_overrides")
    @patch("core_runtime.paths.discover_pack_locations", create=True)
    def test_packs_do_not_mark_pending_approval_as_critical_change(
        self, mock_discover, mock_read_overrides
    ):
        class FakeApprovalManager:
            def get_status(self, pack_id):
                return SimpleNamespace(value="installed")

            def is_pack_approved_and_verified(self, pack_id):
                return False, "not_approved"

            def verify_hash_detailed(self, pack_id, use_cache=True):
                raise AssertionError("pending approval should not run detailed hash verification")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            eco_path = root / "ecosystem.json"
            eco_path.write_text(json.dumps({
                "pack_id": "pack_a",
                "enabled": True,
                "metadata": {"name": "Pack A"},
            }), encoding="utf-8")
            mock_read_overrides.return_value = {}
            mock_discover.return_value = [
                SimpleNamespace(pack_id="pack_a", ecosystem_json_path=eco_path)
            ]

            handler = _FakeHandler()
            handler.approval_manager = FakeApprovalManager()
            result = handler._panel_get_packs()

            pack = next(p for p in result["packs"] if p["pack_id"] == "pack_a")
            self.assertEqual(pack["approval_status"], "installed")
            self.assertFalse(pack["approved"])
            self.assertEqual(pack["approval_reason"], "not_approved")
            self.assertIsNone(pack["hash_valid"])
            self.assertIsNone(pack["critical_changed"])
            self.assertEqual(pack["approval_issues"], ["not_approved"])

    def test_approve_pack_returns_a_verified_approval_state(self):
        class FakeApprovalManager:
            def approve(self, pack_id):
                self.pack_id = pack_id
                return SimpleNamespace(
                    success=True,
                    error=None,
                    status=SimpleNamespace(value="approved"),
                )

        manager = FakeApprovalManager()
        handler = _FakeHandler()
        with patch(
            "core_runtime.approval_manager.get_approval_manager",
            return_value=manager,
        ):
            result = handler._panel_approve_pack("pack_a")

        self.assertEqual(manager.pack_id, "pack_a")
        self.assertEqual(result["pack_id"], "pack_a")
        self.assertTrue(result["approved"])
        self.assertEqual(result["approval_status"], "approved")


class TestPanelGetFlows(unittest.TestCase):
    """GET /api/panel/flows のレスポンス形式テスト"""

    def test_flows_returns_list_and_count_without_kernel(self):
        handler = _FakeHandler()
        result = handler._panel_get_flows()
        self.assertIn("flows", result)
        self.assertIn("count", result)
        self.assertIsInstance(result["flows"], list)
        # kernel なしなので空リスト
        self.assertEqual(result["count"], 0)

    def test_flows_use_flow_loader_source_metadata(self):
        class FakeRegistry:
            def list(self, include_meta=False):
                return {
                    "flow.defaultspack.compile": {
                        "last_meta": {
                            "_source_file": str(
                                Path("ecosystem")
                                / "defaultspack"
                                / "flows"
                                / "compile.flow.yaml"
                            )
                        }
                    }
                }

        handler = _FakeHandler()
        handler.kernel = SimpleNamespace(interface_registry=FakeRegistry())
        result = handler._panel_get_flows()

        self.assertEqual(result["flows"][0]["filename"], "compile.flow.yaml")
        self.assertEqual(result["flows"][0]["pack_id"], "defaultspack")


class TestPanelGetFlowDetail(unittest.TestCase):
    """GET /api/panel/flows/{id} のレスポンス形式テスト"""

    def test_flow_detail_without_kernel_returns_error(self):
        handler = _FakeHandler()
        result = handler._panel_get_flow_detail("test.flow")
        self.assertIn("error", result)
        self.assertIn("status_code", result)
        self.assertEqual(result["status_code"], 503)

    def test_flow_detail_reads_yaml_from_flow_loader_source_file(self):
        class FakeRegistry:
            def __init__(self, source_file):
                self.source_file = source_file

            def list(self, include_meta=False):
                return {
                    "flow.test.flow": {
                        "last_meta": {"_source_file": str(self.source_file)}
                    }
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            source_file = Path(tmpdir) / "test.flow.yaml"
            source_file.write_text(
                "flow_id: test.flow\nsteps:\n  - id: work\n",
                encoding="utf-8",
            )
            handler = _FakeHandler()
            handler.kernel = SimpleNamespace(
                interface_registry=FakeRegistry(source_file)
            )

            result = handler._panel_get_flow_detail("test.flow")

        self.assertEqual(result["filename"], "test.flow.yaml")
        self.assertIn("id: work", result["yaml_content"])


class TestPanelCreateFlow(unittest.TestCase):
    """POST /api/panel/flows のバリデーションテスト"""

    def test_create_flow_missing_flow_id(self):
        handler = _FakeHandler()
        result = handler._panel_create_flow({"yaml_content": "test: true"})
        self.assertIn("error", result)
        self.assertEqual(result["status_code"], 400)

    def test_create_flow_missing_yaml_content(self):
        handler = _FakeHandler()
        result = handler._panel_create_flow({"flow_id": "test_flow"})
        self.assertIn("error", result)
        self.assertEqual(result["status_code"], 400)

    def test_create_flow_invalid_flow_id(self):
        handler = _FakeHandler()
        result = handler._panel_create_flow({
            "flow_id": "../../../etc/passwd",
            "yaml_content": "test: true",
        })
        self.assertIn("error", result)
        self.assertEqual(result["status_code"], 400)


class TestPanelGetVersion(unittest.TestCase):
    """GET /api/panel/version のレスポンス形式テスト"""

    def test_version_returns_required_keys(self):
        handler = _FakeHandler()
        result = handler._panel_get_version()
        self.assertEqual(result["display_version"], APP_DISPLAY_VERSION)
        self.assertEqual(result["app_version"], APP_DISPLAY_VERSION)
        self.assertIn("kernel_version", result)
        self.assertIn("python_version", result)
        self.assertIn("platform", result)
        self.assertIsInstance(result["kernel_version"], str)
        self.assertIsInstance(result["python_version"], str)


class TestPanelCheckUpdates(unittest.TestCase):
    """GET /api/panel/updates のテスト"""

    def test_update_check_failure_returns_non_fatal_snapshot(self):
        handler = _FakeHandler()

        class FakeUpdateManager:
            repo = "example/rumiai"

            def check_many(self, targets):
                raise GitHubUpdateError("release metadata unavailable")

            def current_version(self, target):
                return {"tobkiri": "1.10.0", "defaultspack": "2.0.0"}[target]

        with patch(
            "core_runtime.github_update_manager.get_github_update_manager",
            return_value=FakeUpdateManager(),
        ):
            result = handler._panel_check_updates()

        self.assertNotIn("status_code", result)
        self.assertEqual(result["check_error"], "release metadata unavailable")
        self.assertEqual(len(result["updates"]), 2)
        self.assertEqual(result["updates"][0]["target"], "tobkiri")
        self.assertEqual(result["updates"][0]["current_version"], "1.10.0")
        self.assertEqual(result["updates"][0]["latest_version"], "1.10.0")
        self.assertFalse(result["updates"][0]["update_available"])


class TestPanelGetProfile(unittest.TestCase):
    """GET /api/panel/settings/profile のテスト"""

    def test_profile_not_found_returns_error(self):
        handler = _FakeHandler()
        # profile.json が存在しない場合
        with patch.object(
            ControlPanelHandlersMixin,
            "_panel_read_profile",
            return_value=None,
        ):
            result = handler._panel_get_profile()
            self.assertIn("error", result)
            self.assertEqual(result["status_code"], 404)

    def test_profile_found_returns_data(self):
        handler = _FakeHandler()
        mock_profile = {"username": "test", "language": "ja"}
        with patch.object(
            ControlPanelHandlersMixin,
            "_panel_read_profile",
            return_value=mock_profile,
        ):
            result = handler._panel_get_profile()
            self.assertIn("profile", result)
            self.assertEqual(result["profile"]["username"], "test")


class TestPanelRestartKernel(unittest.TestCase):
    """POST /api/panel/kernel/restart のレスポンス形式テスト"""

    def test_restart_returns_restarting(self):
        control_panel_handlers._last_restart_time = 0.0
        control_panel_handlers.clear_kernel_restart_request()
        handler = _FakeHandler()
        result = handler._panel_restart_kernel()
        self.assertIn("restarting", result)
        self.assertTrue(result["restarting"])
        self.assertTrue(control_panel_handlers.is_kernel_restart_requested())

    def test_restart_sets_graceful_shutdown_flag(self):
        control_panel_handlers._last_restart_time = 0.0
        control_panel_handlers.clear_kernel_restart_request()
        handler = _FakeHandler()
        handler._panel_restart_kernel()
        self.assertTrue(control_panel_handlers.is_kernel_restart_requested())


class TestPanelEnableDisablePack(unittest.TestCase):
    """POST /api/panel/packs/{id}/enable|disable のテスト"""

    @patch("core_runtime.paths.discover_pack_locations", create=True)
    def test_enable_pack_not_found(self, mock_discover):
        mock_discover.return_value = []
        handler = _FakeHandler()
        result = handler._panel_enable_pack("nonexistent_pack")
        self.assertIn("error", result)
        self.assertEqual(result["status_code"], 404)

    @patch.object(ControlPanelHandlersMixin, "_panel_pack_overrides_path")
    @patch("core_runtime.paths.discover_pack_locations", create=True)
    def test_disable_pack_writes_overlay_without_touching_ecosystem(
        self, mock_discover, mock_overrides_path
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            overrides_path = root / "pack_enabled_overrides.json"
            eco_path = root / "ecosystem.json"
            eco_path.write_text(json.dumps({
                "pack_id": "pack_a",
                "enabled": True,
                "metadata": {"name": "Pack A"},
            }), encoding="utf-8")
            mock_overrides_path.return_value = overrides_path
            mock_discover.return_value = [
                SimpleNamespace(pack_id="pack_a", ecosystem_json_path=eco_path)
            ]

            handler = _FakeHandler()
            result = handler._panel_disable_pack("pack_a")

            self.assertEqual(result["enabled"], False)
            eco = json.loads(eco_path.read_text(encoding="utf-8"))
            self.assertTrue(eco["enabled"])
            self.assertEqual(
                json.loads(overrides_path.read_text(encoding="utf-8")),
                {"pack_a": False},
            )

    @patch.object(ControlPanelHandlersMixin, "_panel_read_pack_overrides")
    @patch("core_runtime.paths.discover_pack_locations", create=True)
    def test_list_packs_applies_overlay_enabled_value(
        self, mock_discover, mock_read_overrides
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            eco_path = root / "ecosystem.json"
            eco_path.write_text(json.dumps({
                "pack_id": "pack_a",
                "enabled": True,
                "metadata": {"name": "Pack A"},
            }), encoding="utf-8")
            mock_read_overrides.return_value = {"pack_a": False}
            mock_discover.return_value = [
                SimpleNamespace(pack_id="pack_a", ecosystem_json_path=eco_path)
            ]

            handler = _FakeHandler()
            result = handler._panel_get_packs()

            pack = next(p for p in result["packs"] if p["pack_id"] == "pack_a")
            self.assertFalse(pack["enabled"])


class TestStartupProfileHandlers(unittest.TestCase):
    def test_startup_profile_manager_uses_kernel_runtime_dependencies_as_fallback(self):
        handler = _FakeHandler()
        interface_registry = object()
        approval_manager = object()
        handler.kernel = SimpleNamespace(
            interface_registry=interface_registry,
            approval_manager=approval_manager,
            ecosystem_dir="/tmp/fake-ecosystem",
        )

        manager = handler._panel_startup_profile_manager()

        self.assertIs(manager.interface_registry, interface_registry)
        self.assertIs(manager.approval_manager, approval_manager)
        self.assertEqual(manager.ecosystem_dir, "/tmp/fake-ecosystem")

    def test_get_startup_profiles_returns_manager_payload(self):
        handler = _FakeHandler()
        payload = {"profiles": [], "active_profile_id": None, "catalog": {}, "last_launched_profile_id": None}
        with patch.object(
            ControlPanelHandlersMixin,
            "_panel_startup_profile_manager",
        ) as mock_factory:
            mock_factory.return_value.list_profiles_payload.return_value = payload
            result = handler._panel_get_startup_profiles()
        self.assertEqual(result, payload)

    def test_launch_startup_profile_forwards_to_manager(self):
        handler = _FakeHandler()
        payload = {
            "profile": {"profile_id": "p1"},
            "launched": True,
            "restart_requested": True,
            "handoff": {"kind": "kernel_restart"},
        }
        with patch.object(
            ControlPanelHandlersMixin,
            "_panel_startup_profile_manager",
        ) as mock_factory:
            mock_factory.return_value.launch_profile.return_value = payload
            result = handler._panel_launch_startup_profile("p1")
        self.assertEqual(result["profile"]["profile_id"], "p1")
        self.assertTrue(result["launched"])
        self.assertTrue(result["restart_requested"])

    def test_delete_startup_profile_forwards_to_manager(self):
        handler = _FakeHandler()
        payload = {"deleted": True, "deleted_profile_id": "p1", "active_profile_id": "default-profile"}
        with patch.object(
            ControlPanelHandlersMixin,
            "_panel_startup_profile_manager",
        ) as mock_factory:
            mock_factory.return_value.delete_profile.return_value = payload
            result = handler._panel_delete_startup_profile("p1")
        self.assertTrue(result["deleted"])
        self.assertEqual(result["active_profile_id"], "default-profile")


if __name__ == "__main__":
    unittest.main()
