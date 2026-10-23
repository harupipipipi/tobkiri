"""
test_phase_a_setup_api.py - /api/setup/status, /api/setup/complete のテスト

AppLifecycleManager の check_setup_status() / complete_setup() と
PackAPIHandler のセットアップ API エンドポイントをテストする。
"""

import json
from io import BytesIO
from unittest.mock import MagicMock


class TestCheckSetupStatus:
    """AppLifecycleManager.check_setup_status() のテスト"""

    def test_clean_home_runs_canonical_profile_transaction(self, tmp_path):
        """A clean home is bootstrapped without a legacy setup Profile."""
        from core_runtime.app_lifecycle_manager import AppLifecycleManager
        alm = AppLifecycleManager(base_dir=tmp_path)
        result = alm.check_setup_status()
        assert result["needs_setup"] is True
        assert result["setup_state"] == "profile_transaction_required"

    def test_not_needs_setup_when_profile_valid(self, tmp_path):
        """profile.json が有効 -> needs_setup: False"""
        from core_runtime.app_lifecycle_manager import AppLifecycleManager

        settings_dir = tmp_path / "user_data" / "settings"
        settings_dir.mkdir(parents=True)
        profile = {
            "schema_version": 1,
            "initialized_at": "2026-03-16T12:00:00Z",
            "username": "testuser",
            "language": "ja",
            "icon": None,
            "occupation": None,
            "setup_completed": True,
        }
        (settings_dir / "profile.json").write_text(
            json.dumps(profile), encoding="utf-8"
        )

        alm = AppLifecycleManager(base_dir=tmp_path)
        result = alm.check_setup_status()
        assert result["needs_setup"] is True
        assert result["reason"] == "explicit_bootstrap_confirmation_required"
        assert result["host_catalog_verified"] is True
        assert result["profile_ceremony_available"] is False
        assert result["defaults_bootstrap_required"] is True
        assert result["launch_ready"] is False

    def test_setup_status_includes_runtime_readiness(self, tmp_path):
        from core_runtime.app_lifecycle_manager import (
            AppLifecycleManager,
            mark_panel_ready,
            reset_runtime_readiness,
        )

        reset_runtime_readiness()
        mark_panel_ready()

        alm = AppLifecycleManager(base_dir=tmp_path)
        result = alm.check_setup_status()

        assert result["panel_ready"] is True
        assert result["runtime_ready"] is False
        assert result["runtime_status"] == "panel_ready"


class TestCompleteSetup:
    """AppLifecycleManager.complete_setup() のテスト"""

    def test_complete_setup_valid(self, tmp_path):
        """有効なデータでセットアップ完了"""
        from core_runtime.app_lifecycle_manager import AppLifecycleManager
        alm = AppLifecycleManager(base_dir=tmp_path)
        result = alm.complete_setup({
            "username": "testuser",
            "language": "ja",
        })
        assert result["success"] is False
        assert result["setup_state"] == "profile_transaction_failed"

    def test_complete_setup_no_username(self, tmp_path):
        """username が空 -> エラー"""
        from core_runtime.app_lifecycle_manager import AppLifecycleManager
        alm = AppLifecycleManager(base_dir=tmp_path)
        result = alm.complete_setup({
            "username": "",
            "language": "ja",
        })
        assert result["success"] is False
        assert len(result["errors"]) > 0

    def test_complete_setup_bad_language(self, tmp_path):
        """language が不正 -> エラー"""
        from core_runtime.app_lifecycle_manager import AppLifecycleManager
        alm = AppLifecycleManager(base_dir=tmp_path)
        result = alm.complete_setup({
            "username": "testuser",
            "language": "xx",
        })
        assert result["success"] is False
        assert len(result["errors"]) > 0

    def test_complete_setup_missing_username(self, tmp_path):
        """username が無い -> エラー"""
        from core_runtime.app_lifecycle_manager import AppLifecycleManager
        alm = AppLifecycleManager(base_dir=tmp_path)
        result = alm.complete_setup({
            "language": "ja",
        })
        assert result["success"] is False

    def test_complete_setup_with_optional_fields(self, tmp_path):
        """オプションフィールド付きでセットアップ完了"""
        from core_runtime.app_lifecycle_manager import AppLifecycleManager
        alm = AppLifecycleManager(base_dir=tmp_path)
        result = alm.complete_setup({
            "username": "testuser",
            "language": "en",
            "icon": "/path/to/icon.png",
            "occupation": "Developer",
        })
        assert result["success"] is False
        assert result["setup_state"] == "profile_transaction_failed"

    def test_setup_status_no_auth_required(self):
        """/api/setup/status は認証前に処理されること。"""
        from core_runtime.pack_api_server import PackAPIHandler

        handler = object.__new__(PackAPIHandler)
        handler.path = "/api/setup/status"
        handler.client_address = ("198.51.100.7", 12345)
        handler._send_response = MagicMock()
        handler._check_auth = MagicMock(side_effect=AssertionError("auth should not run"))
        handler._match_web_mount = MagicMock(return_value=None)
        handler._check_rate_limit = MagicMock(return_value=True)
        handler._is_pre_auth_route = MagicMock(return_value=True)
        PackAPIHandler.app_lifecycle_manager = MagicMock()
        PackAPIHandler.app_lifecycle_manager.check_setup_status.return_value = {
            "needs_setup": True,
        }

        PackAPIHandler.do_GET(handler)

        handler._send_response.assert_called_once()
        handler._check_auth.assert_not_called()

    def test_setup_complete_is_typed_retired_route_without_writes(self, tmp_path):
        """The legacy completion route is 410 for every auth state and writes nothing."""
        from core_runtime.app_lifecycle_manager import AppLifecycleManager
        from core_runtime.pack_api_server import PackAPIHandler

        lifecycle = AppLifecycleManager(base_dir=tmp_path)
        lifecycle.complete_setup = MagicMock(
            side_effect=AssertionError("retired route must not capture a profile")
        )
        PackAPIHandler.app_lifecycle_manager = lifecycle

        for authorization in (None, "Bearer authenticated-test-token"):
            handler = object.__new__(PackAPIHandler)
            handler.path = "/api/setup/complete"
            handler.headers = {
                "Content-Length": "2",
                **({"Authorization": authorization} if authorization else {}),
            }
            handler.client_address = ("198.51.100.7", 12345)
            handler._send_response = MagicMock()
            handler._check_auth = MagicMock(
                side_effect=AssertionError("retired barrier runs before auth")
            )
            handler._check_rate_limit = MagicMock(return_value=True)
            handler._discard_request_body = MagicMock()
            handler._parse_body = MagicMock(
                side_effect=AssertionError("retired body must not be parsed")
            )

            PackAPIHandler.do_POST(handler)

            response, status = handler._send_response.call_args.args
            assert status == 410
            assert response.success is False
            assert response.data == {
                "state": "legacy_setup_retired",
                "action": "install_defaults_profile",
                "setup_api_version": "io.tobkiri.setup-state.v4",
                "retired_route": "/api/setup/complete",
                "write_set": [],
            }
            handler._check_auth.assert_not_called()
            handler._parse_body.assert_not_called()

        lifecycle.complete_setup.assert_not_called()
        assert list(tmp_path.rglob("*")) == []

    def test_setup_packs_list_no_auth_required_during_initial_setup(self):
        from core_runtime.pack_api_server import PackAPIHandler

        handler = object.__new__(PackAPIHandler)
        handler.path = "/api/setup/packs"
        handler.client_address = ("198.51.100.7", 12345)
        handler._match_web_mount = MagicMock(return_value=None)
        handler._check_auth = MagicMock(side_effect=AssertionError("auth should not run"))
        handler._setup_list_packs = MagicMock(return_value={"packs": []})
        handler._send_mapping_result = MagicMock()
        handler._send_response = MagicMock()
        PackAPIHandler.app_lifecycle_manager = MagicMock()
        PackAPIHandler.app_lifecycle_manager.check_setup_status.return_value = {
            "needs_setup": True,
        }

        PackAPIHandler.do_GET(handler)

        handler._check_auth.assert_not_called()
        handler._send_mapping_result.assert_called_once_with({"packs": []})

    def test_setup_packs_list_requires_auth_after_setup_completed(self):
        from core_runtime.pack_api_server import PackAPIHandler

        handler = object.__new__(PackAPIHandler)
        handler.path = "/api/setup/packs"
        handler.client_address = ("198.51.100.7", 12345)
        handler._match_web_mount = MagicMock(return_value=None)
        handler._check_auth = MagicMock(return_value=False)
        handler._send_response = MagicMock()
        PackAPIHandler.app_lifecycle_manager = MagicMock()
        PackAPIHandler.app_lifecycle_manager.check_setup_status.return_value = {
            "needs_setup": False,
        }

        PackAPIHandler.do_GET(handler)

        handler._check_auth.assert_called_once_with("GET", "/api/setup/packs")

    def test_setup_migration_status_no_auth_required_during_initial_setup(self):
        from core_runtime.pack_api_server import PackAPIHandler

        handler = object.__new__(PackAPIHandler)
        handler.path = "/api/setup/migration/status"
        handler.client_address = ("198.51.100.7", 12345)
        handler._match_web_mount = MagicMock(return_value=None)
        handler._check_auth = MagicMock(side_effect=AssertionError("auth should not run"))
        handler._setup_get_migration_status = MagicMock(
            return_value={"error": "retired", "status_code": 410}
        )
        handler._send_mapping_result = MagicMock()
        handler._send_response = MagicMock()
        PackAPIHandler.app_lifecycle_manager = MagicMock()
        PackAPIHandler.app_lifecycle_manager.check_setup_status.return_value = {
            "needs_setup": True,
        }

        PackAPIHandler.do_GET(handler)

        handler._check_auth.assert_not_called()
        handler._send_mapping_result.assert_called_once_with(
            {"error": "retired", "status_code": 410}
        )

    def test_setup_migration_status_remains_retired_after_setup_completed(self):
        from core_runtime.pack_api_server import PackAPIHandler

        handler = object.__new__(PackAPIHandler)
        handler.path = "/api/setup/migration/status"
        handler.client_address = ("198.51.100.7", 12345)
        handler._match_web_mount = MagicMock(return_value=None)
        handler._check_auth = MagicMock(side_effect=AssertionError("auth should not run"))
        handler._setup_get_migration_status = MagicMock(
            return_value={"error": "retired", "status_code": 410}
        )
        handler._send_mapping_result = MagicMock()
        handler._send_response = MagicMock()
        PackAPIHandler.app_lifecycle_manager = MagicMock()
        PackAPIHandler.app_lifecycle_manager.check_setup_status.return_value = {
            "needs_setup": False,
        }

        PackAPIHandler.do_GET(handler)

        handler._check_auth.assert_not_called()
        handler._send_mapping_result.assert_called_once_with(
            {"error": "retired", "status_code": 410}
        )

    def test_setup_pack_install_no_auth_required_only_during_initial_setup(self):
        from core_runtime.pack_api_server import PackAPIHandler

        handler = object.__new__(PackAPIHandler)
        handler.path = "/api/setup/packs/install"
        handler.client_address = ("198.51.100.7", 12345)
        handler._check_auth = MagicMock(side_effect=AssertionError("auth should not run"))
        body = {"install_defaults_profile": False}
        handler._parse_body = MagicMock(return_value=body)
        handler._setup_install_pack = MagicMock(
            return_value={"error": "retired", "status_code": 410}
        )
        handler.wfile = BytesIO()
        handler._send_mapping_result = MagicMock()
        handler._send_response = MagicMock()
        PackAPIHandler.app_lifecycle_manager = MagicMock()
        PackAPIHandler.app_lifecycle_manager.check_setup_status.return_value = {
            "needs_setup": True,
        }

        PackAPIHandler.do_POST(handler)

        handler._check_auth.assert_not_called()
        handler._setup_install_pack.assert_called_once_with(body)
        handler._send_mapping_result.assert_called_once_with(
            {"error": "retired", "status_code": 410}
        )

    def test_setup_pack_install_requires_auth_after_setup_completed(self):
        from core_runtime.pack_api_server import PackAPIHandler

        handler = object.__new__(PackAPIHandler)
        handler.path = "/api/setup/packs/install"
        handler.client_address = ("198.51.100.7", 12345)
        handler._check_auth = MagicMock(return_value=False)
        handler._discard_request_body = MagicMock()
        handler._send_response = MagicMock()
        PackAPIHandler.app_lifecycle_manager = MagicMock()
        PackAPIHandler.app_lifecycle_manager.check_setup_status.return_value = {
            "needs_setup": False,
        }

        PackAPIHandler.do_POST(handler)

        handler._check_auth.assert_called_once_with("POST", "/api/setup/packs/install")
        handler._discard_request_body.assert_called_once()


class TestHealthPayload:
    def test_health_reports_runtime_error(self, tmp_path):
        from core_runtime.app_lifecycle_manager import (
            AppLifecycleManager,
            mark_runtime_failed,
            reset_runtime_readiness,
        )

        reset_runtime_readiness()
        mark_runtime_failed("runtime crashed")

        alm = AppLifecycleManager(base_dir=tmp_path)
        result = alm.get_health()

        assert result["status"] == "error"
        assert result["runtime_status"] == "error"
        assert result["runtime_error"] == "runtime crashed"
        reset_runtime_readiness()
