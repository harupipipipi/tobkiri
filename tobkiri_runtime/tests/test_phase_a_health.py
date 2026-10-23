"""
test_phase_a_health.py - /health エンドポイントのテスト

AppLifecycleManager の get_health() と
PackAPIHandler の /health エンドポイントをテストする。
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
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

    def test_reconfirmation_health_remains_setup_required(
        self, tmp_path, monkeypatch
    ):
        """A control-only capture cannot be projected as launch-ready."""

        from core_runtime import app_lifecycle_manager as lifecycle_module
        from core_runtime.bootstrap import profile_capture

        diagnostic = "ResolvedPlan binding lacks a verified artifact"
        monkeypatch.setattr(
            lifecycle_module,
            "get_runtime_readiness",
            lambda: {
                "panel_ready": True,
                "runtime_ready": False,
                "runtime_status": "profile_reconfirmation_required",
                "runtime_error": diagnostic,
            },
        )
        monkeypatch.setattr(
            profile_capture,
            "active_profile_exists",
            lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("reconfirmation health recaptured the Profile")
            ),
        )

        result = lifecycle_module.AppLifecycleManager(base_dir=tmp_path).get_health()

        assert result["needs_setup"] is True
        assert result["runtime_ready"] is False
        assert result["runtime_status"] == "profile_reconfirmation_required"
        assert result["runtime_error"] == diagnostic
        assert result["host_catalog_verified"] is True
        assert result["profile_ceremony_available"] is True
        assert result["active_profile_ready"] is False
        assert result["launch_ready"] is False

    def test_pack_activation_health_stays_pending_until_host_pointer_is_published(
        self, tmp_path, monkeypatch
    ):
        from core_runtime import app_lifecycle_manager as lifecycle_module
        from core_runtime import pack_control_v4

        alm = lifecycle_module.AppLifecycleManager(base_dir=tmp_path)
        monkeypatch.setattr(
            alm,
            "check_setup_status",
            lambda: (_ for _ in ()).throw(AssertionError("captured during Pack activation")),
        )
        monkeypatch.setattr(
            lifecycle_module,
            "get_runtime_readiness",
            lambda: {"panel_ready": True},
        )
        monkeypatch.setattr(
            pack_control_v4,
            "resolve_profile_pack_set",
            lambda _pack_ids: SimpleNamespace(plan={"plan_digest": "sha256:" + "a" * 64}),
        )
        observed = []
        monkeypatch.setattr(
            pack_control_v4,
            "activate_resolved_profile_pack_set",
            lambda *_args, **_kwargs: observed.append(alm.get_health()),
        )

        pack_control_v4._activate_pack_set(
            {
                "resolved_profile": {"profile_id": "defaults"},
                "resolved_plan": {
                    "profile_revision": "sha256:" + "b" * 64,
                    "plan_digest": "sha256:" + "c" * 64,
                },
                "activation": {"activation_id": "activation:predecessor"},
            },
            ["example_pack"],
        )

        assert len(observed) == 1
        assert observed[0]["status"] == "ok"
        assert observed[0]["runtime_status"] == "panel_ready"
        assert observed[0]["runtime_ready"] is False
        assert observed[0]["launch_ready"] is False

    def test_health_disregards_capture_that_overlapped_a_pack_transition(
        self, tmp_path, monkeypatch
    ):
        from core_runtime import app_lifecycle_manager as lifecycle_module

        alm = lifecycle_module.AppLifecycleManager(base_dir=tmp_path)
        monkeypatch.setattr(
            lifecycle_module,
            "get_runtime_readiness",
            lambda: {"panel_ready": True},
        )

        def overlapping_capture():
            with lifecycle_module.pack_profile_transition():
                pass
            return {"runtime_status": "error", "runtime_error": "stale capture"}

        monkeypatch.setattr(alm, "check_setup_status", overlapping_capture)
        result = alm.get_health()

        assert result["status"] == "ok"
        assert result["runtime_status"] == "panel_ready"
        assert result["runtime_error"] is None
        assert result["launch_ready"] is False

    def test_overlapping_pack_activation_does_not_log_a_false_setup_failure(
        self, tmp_path, monkeypatch, caplog
    ):
        from core_runtime import app_lifecycle_manager as lifecycle_module
        from core_runtime import profile_runtime_port
        from core_runtime.bootstrap import profile_capture

        alm = lifecycle_module.AppLifecycleManager(base_dir=tmp_path)
        monkeypatch.setattr(profile_capture, "active_profile_exists", lambda **_kwargs: True)
        monkeypatch.setattr(
            profile_runtime_port,
            "require_profile_runtime",
            lambda: SimpleNamespace(),
        )

        def overlapping_capture(**_kwargs):
            with lifecycle_module.pack_profile_transition():
                pass
            raise RuntimeError("Host active pointer does not match the Profile activation")

        monkeypatch.setattr(profile_capture, "capture_active_profile", overlapping_capture)
        monkeypatch.setattr(
            lifecycle_module,
            "get_runtime_readiness",
            lambda: {"panel_ready": True},
        )

        result = alm.check_setup_status()

        assert result["reason"] == "pack_profile_transition_in_progress"
        assert result["runtime_ready"] is False
        assert result["launch_ready"] is False
        assert "canonical v4 setup status failed" not in caplog.text

    def test_health_during_activation_does_not_recapture_profile(self, tmp_path, monkeypatch):
        from core_runtime import app_lifecycle_manager as lifecycle_module

        alm = lifecycle_module.AppLifecycleManager(base_dir=tmp_path)
        monkeypatch.setattr(
            alm,
            "check_setup_status",
            lambda: (_ for _ in ()).throw(AssertionError("profile recaptured")),
        )
        monkeypatch.setattr(
            lifecycle_module,
            "get_runtime_readiness",
            lambda: {"panel_ready": True},
        )

        with alm._activation_lock:
            result = alm.get_health()

        assert result["status"] == "ok"
        assert result["needs_setup"] is True
        assert result["panel_ready"] is True
        assert result["runtime_ready"] is False
        assert result["runtime_status"] == "panel_ready"
        assert result["launch_ready"] is False

    def test_parallel_health_does_not_recapture_profile(self, tmp_path, monkeypatch):
        from core_runtime import app_lifecycle_manager as lifecycle_module

        alm = lifecycle_module.AppLifecycleManager(base_dir=tmp_path)
        monkeypatch.setattr(
            alm,
            "check_setup_status",
            lambda: (_ for _ in ()).throw(AssertionError("profile recaptured")),
        )
        monkeypatch.setattr(
            lifecycle_module,
            "get_runtime_readiness",
            lambda: {"panel_ready": True},
        )

        with alm._health_capture_lock:
            result = alm.get_health()

        assert result["needs_setup"] is True
        assert result["runtime_ready"] is False
        assert result["launch_ready"] is False

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
