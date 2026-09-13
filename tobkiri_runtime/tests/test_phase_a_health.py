"""
test_phase_a_health.py - /health エンドポイントのテスト

AppLifecycleManager の get_health() と
PackAPIHandler の /health エンドポイントをテストする。
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

# core_setup のパスを追加
_CORE_SETUP_DIR = (
    Path(__file__).resolve().parent.parent
    / "core_runtime"
    / "core_pack"
    / "core_setup"
)
if str(_CORE_SETUP_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_SETUP_DIR))


class TestAppLifecycleManagerHealth:
    """AppLifecycleManager.get_health() のテスト"""

    def test_health_needs_setup_true(self, tmp_path):
        """A fresh home requires explicit canonical Defaults v4 confirmation."""
        from core_runtime.app_lifecycle_manager import AppLifecycleManager
        alm = AppLifecycleManager(base_dir=tmp_path)
        result = alm.get_health()
        assert result["status"] == "ok"
        assert result["needs_setup"] is True

    def test_health_ignores_legacy_profile_json(self, tmp_path):
        """A legacy profile.json cannot activate Defaults v4."""
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
        result = alm.get_health()
        assert result["status"] == "ok"
        assert result["needs_setup"] is True

    def test_health_ignores_legacy_setup_pack_selection(self, tmp_path):
        """A legacy setup selection cannot activate Defaults v4."""
        from core_runtime.app_lifecycle_manager import AppLifecycleManager

        setup_pack_dir = tmp_path / "ecosystem" / "setup_pack" / "defaultspack"
        setup_pack_dir.mkdir(parents=True)
        (setup_pack_dir / "pack.json").write_text(
            json.dumps(
                {
                    "pack_id": "defaultspack",
                    "display_name": "Default Pack",
                    "description": "desc",
                    "target_pack_id": "defaultspack",
                    "version": "1.0.0",
                    "supports_all_ok": True,
                }
            ),
            encoding="utf-8",
        )
        target_dir = tmp_path / "ecosystem" / "defaultspack"
        target_dir.mkdir(parents=True)
        (target_dir / "ecosystem.json").write_text(
            json.dumps({"pack_identity": "rumi:ecosystem/defaultspack"}),
            encoding="utf-8",
        )
        settings_dir = tmp_path / "user_data" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "setup_pack_selection.json").write_text(
            json.dumps(
                {
                    "setup_pack_id": "defaultspack",
                    "target_pack_id": "defaultspack",
                    "setup_pack_ids": ["defaultspack"],
                    "target_pack_ids": ["defaultspack"],
                    "active_setup_pack_id": "defaultspack",
                    "active_target_pack_id": "defaultspack",
                }
            ),
            encoding="utf-8",
        )

        alm = AppLifecycleManager(base_dir=tmp_path)
        result = alm.get_health()

        assert result["status"] == "ok"
        assert result["needs_setup"] is True

    def test_health_does_not_accept_stale_setup_pack_selection(self, tmp_path):
        """Stale legacy selection cannot override canonical Defaults v4 state."""
        from core_runtime.app_lifecycle_manager import AppLifecycleManager

        settings_dir = tmp_path / "user_data" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "setup_pack_selection.json").write_text(
            json.dumps(
                {
                    "setup_pack_id": "ghost_pack",
                    "target_pack_id": "ghost_pack",
                    "setup_pack_ids": ["ghost_pack"],
                    "target_pack_ids": ["ghost_pack"],
                    "active_setup_pack_id": "ghost_pack",
                    "active_target_pack_id": "ghost_pack",
                }
            ),
            encoding="utf-8",
        )

        alm = AppLifecycleManager(base_dir=tmp_path)
        result = alm.get_health()

        assert result["status"] == "ok"
        assert result["needs_setup"] is True

    def test_health_returns_ok_status(self, tmp_path):
        """get_health() は常に status=ok を返す"""
        from core_runtime.app_lifecycle_manager import AppLifecycleManager
        alm = AppLifecycleManager(base_dir=tmp_path)
        result = alm.get_health()
        assert "status" in result
        assert "needs_setup" in result
        assert result["status"] == "ok"

    def test_health_no_auth_required(self):
        """/health は認証前に処理されること。"""
        from core_runtime.pack_api_server import PackAPIHandler

        handler = object.__new__(PackAPIHandler)
        handler.path = "/health"
        handler.client_address = ("198.51.100.7", 12345)
        handler._send_response = MagicMock()
        handler._check_auth = MagicMock(side_effect=AssertionError("auth should not run"))
        handler._match_web_mount = MagicMock(return_value=None)
        handler._check_rate_limit = MagicMock(return_value=True)
        PackAPIHandler.app_lifecycle_manager = MagicMock()
        PackAPIHandler.app_lifecycle_manager.get_health.return_value = {"status": "ok"}

        PackAPIHandler.do_GET(handler)

        handler._send_response.assert_called_once()
        handler._check_auth.assert_not_called()
