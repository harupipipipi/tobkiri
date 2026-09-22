from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


class TestDefaultspackDesktopSurface(unittest.TestCase):
    _PANEL_BOOTSTRAP_SECRET = "desktop-surface-host-secret"

    def test_launch_log_uses_external_app_data_and_rejects_sealed_resources(self):
        from defaultspack import desktop_app

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sealed_app = root / "sealed-app"
            sealed_app.mkdir()
            external_logs = root / "app-data" / "logs"
            with patch.object(
                desktop_app,
                "_sealed_app_root",
                return_value=sealed_app,
            ):
                with patch.dict(
                    os.environ,
                    {"RUMI_LOG_DIR": str(external_logs)},
                    clear=True,
                ):
                    self.assertEqual(
                        desktop_app._diagnostic_log_path(),
                        (external_logs / "defaultspack-launch.jsonl").resolve(),
                    )
                with patch.dict(
                    os.environ,
                    {"RUMI_LOG_DIR": str(sealed_app / "logs")},
                    clear=True,
                ):
                    with self.assertRaisesRegex(ValueError, "outside sealed app"):
                        desktop_app._diagnostic_log_path()

                redirect = root / "redirect"
                redirect.symlink_to(sealed_app, target_is_directory=True)
                with patch.dict(
                    os.environ,
                    {"RUMI_LOG_DIR": str(redirect / "logs")},
                    clear=True,
                ):
                    with self.assertRaisesRegex(ValueError, "outside sealed app"):
                        desktop_app._diagnostic_log_path()

    @staticmethod
    def _activate_defaults(user_data: Path) -> None:
        from core_runtime.bootstrap.profile_capture import (
            capture_default_profile,
            prepare_default_profile_confirmation,
        )

        with patch.dict(
            os.environ,
            {
                "TOBKIRI_USER_DATA": str(user_data),
                "RUMI_USER_DATA": str(user_data),
            },
            clear=False,
        ):
            capture_default_profile(
                confirmation=prepare_default_profile_confirmation()
            )
        user_data.chmod(0o700)
        contract_path = user_data / "host_contract.json"
        contract_path.write_text(
            json.dumps(
                {
                    "schema_version": "tobkiri.host-contract.v1",
                    "profile_id": "defaults",
                    "values": {
                        "panel_bootstrap_secret": (
                            TestDefaultspackDesktopSurface._PANEL_BOOTSTRAP_SECRET
                        )
                    },
                }
            ),
            encoding="utf-8",
        )
        contract_path.chmod(0o600)
        from core_runtime.panel_auth import (
            PanelAuthManager,
            reset_panel_auth_manager_for_tests,
        )

        reset_panel_auth_manager_for_tests(
            PanelAuthManager(
                bootstrap_secret=(
                    TestDefaultspackDesktopSurface._PANEL_BOOTSTRAP_SECRET
                )
            )
        )

    def test_desktop_app_help_exits_before_runtime_setup(self):
        from defaultspack import desktop_app

        with patch.object(
            desktop_app, "_ensure_import_path"
        ) as ensure_import_path:
            with self.assertRaises(SystemExit) as exited:
                desktop_app.main(["--help"])

        self.assertEqual(exited.exception.code, 0)
        ensure_import_path.assert_not_called()

    def test_desktop_app_url_uses_canonical_ipv4_loopback(self):
        from defaultspack import desktop_app

        with patch.dict(
            os.environ,
            {
                "DEFAULTS_HTTP_PORT": "18776",
                "RUMI_DEFAULTSPACK_PORT": "18776",
            },
            clear=True,
        ):
            self.assertEqual(
                desktop_app._url(), "http://127.0.0.1:18776/chat"
            )

    def test_debug_own_bind_does_not_adopt_existing_healthy_server(self):
        from defaultspack import desktop_app

        class BindFailureServer:
            def start(self):
                raise OSError("address already in use")

        with tempfile.TemporaryDirectory() as tmp:
            user_data = Path(tmp) / "user_data"
            self._activate_defaults(user_data)
            with patch.dict(
                os.environ,
                {
                    "DEFAULTS_HTTP_HOST": "127.0.0.1",
                    "DEFAULTS_HTTP_PORT": "18776",
                    "RUMI_DEFAULTSPACK_PORT": "18776",
                    "RUMI_DEFAULTSPACK_REQUIRE_OWN_BIND": "1",
                    "TOBKIRI_USER_DATA": str(user_data),
                    "RUMI_USER_DATA": str(user_data),
                    "TOBKIRI_HOST_CONTRACT_PATH": str(
                        user_data / "host_contract.json"
                    ),
                },
                clear=False,
            ):
                with patch(
                    "core_runtime.pack_api_server.PackAPIServer",
                    return_value=BindFailureServer(),
                ):
                    with patch.object(
                        desktop_app, "_wait_until_ready"
                    ) as wait_until_ready:
                        with patch(
                            "domain.scheduler.daemon.start_scheduler_daemon"
                        ) as start_scheduler:
                            with self.assertRaisesRegex(
                                OSError, "address already in use"
                            ):
                                desktop_app.main()

        wait_until_ready.assert_not_called()
        start_scheduler.assert_not_called()

    def test_surface_url_passes_local_auth_only_in_fragment(self):
        from defaultspack import desktop_app

        with patch.dict(
            os.environ,
            {"RUMI_DEFAULTSPACK_LOCAL_TOKEN": "local-token"},
            clear=True,
        ):
            url = desktop_app._surface_url("http://localhost:8766/chat")

        self.assertEqual(url, "http://localhost:8766/chat")
        self.assertNotIn("local-token", url)
        self.assertNotIn("#", url)
        self.assertNotIn("?", url)

    def test_surface_url_reads_launcher_token_file(self):
        from defaultspack import desktop_app

        with tempfile.TemporaryDirectory() as tmp:
            user_data = Path(tmp) / "user_data"
            user_data.mkdir()
            (Path(tmp) / ".desktop_api_token").write_text(
                "local token", encoding="utf-8"
            )
            with patch.dict(
                os.environ,
                {"RUMI_USER_DATA": str(user_data)},
                clear=True,
            ):
                url = desktop_app._surface_url("http://localhost:8766/chat")

        self.assertEqual(url, "http://localhost:8766/chat")
        self.assertNotIn("local token", url)
        self.assertNotIn("#", url)

    def test_desktop_startup_never_imports_bundle_local_legacy_state(self):
        from defaultspack import desktop_app

        with tempfile.TemporaryDirectory() as tmp:
            user_data = Path(tmp) / "user_data"
            bundle_root = Path(tmp) / "replaceable-bundle"
            legacy_root = bundle_root / "user_data"
            legacy_secrets = legacy_root / "secrets"
            legacy_settings = legacy_root / "shared" / "frontend_settings.json"
            legacy_secrets.mkdir(parents=True, exist_ok=True)
            legacy_settings.parent.mkdir(parents=True, exist_ok=True)
            secret_file = legacy_secrets / "OPENROUTER_API_KEY.json"
            key_file = legacy_root / ".secrets_key"
            secret_file.write_text('{"encrypted": "fixture"}', encoding="utf-8")
            key_file.write_text("fixture-key", encoding="utf-8")
            legacy_settings.write_text('{"models": {"preferred_model": "openrouter/demo"}}', encoding="utf-8")
            with patch.dict(os.environ, {"RUMI_USER_DATA": str(user_data)}, clear=True):
                with patch.object(desktop_app, "_pack_root", return_value=bundle_root):
                    desktop_app._configure_persistent_user_state()
                configured_settings = os.environ[
                    "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH"
                ]

            self.assertFalse((user_data / "secrets" / secret_file.name).exists())
            self.assertFalse((user_data / ".secrets_key").exists())
            self.assertFalse(
                (
                    user_data
                    / "defaultspack"
                    / "shared"
                    / "frontend_settings.json"
                ).exists()
            )
            self.assertEqual(
                configured_settings,
                str(
                    user_data
                    / "defaultspack"
                    / "shared"
                    / "frontend_settings.json"
                ),
            )

    def test_surface_can_be_disabled_for_smoke_tests(self):
        from defaultspack.native_webview import open_desktop_surface

        with patch.dict(os.environ, {"RUMI_DEFAULTSPACK_OPEN_BROWSER": "0"}):
            result = open_desktop_surface("http://127.0.0.1:8766/")

        self.assertEqual(result, "disabled")

    def test_webview_surface_is_default_and_does_not_open_browser_when_missing(self):
        from defaultspack.native_webview import open_desktop_surface

        with patch.dict(os.environ, {"RUMI_DEFAULTSPACK_OPEN_BROWSER": "1"}, clear=True):
            with patch.dict(sys.modules, {"webview": None}):
                with patch("webbrowser.open") as mock_open:
                    result = open_desktop_surface("http://127.0.0.1:8766/")

        self.assertEqual(result, "webview_unavailable")
        mock_open.assert_not_called()

    def test_webview_surface_falls_back_when_optional_dependency_is_missing(self):
        from defaultspack.native_webview import open_desktop_surface

        with patch.dict(os.environ, {"RUMI_DEFAULTSPACK_OPEN_BROWSER": "1", "RUMI_DEFAULTSPACK_SURFACE": "webview"}, clear=True):
            with patch.dict(sys.modules, {"webview": None}):
                with patch("webbrowser.open") as mock_open:
                    result = open_desktop_surface("http://127.0.0.1:8766/")

        self.assertEqual(result, "webview_unavailable")
        mock_open.assert_not_called()

    def test_desktop_app_main_stops_server_after_blocking_webview_closes(self):
        from defaultspack import desktop_app

        class FakeServer:
            def __init__(self, facade=None):
                self.started = False
                self.stopped = False

            def start(self):
                self.started = True

            def stop(self):
                self.stopped = True

        fake_server = FakeServer()

        with tempfile.TemporaryDirectory() as tmp:
            user_data = Path(tmp) / "user_data"
            self._activate_defaults(user_data)
            with patch.dict(
                os.environ,
                {
                    "RUMI_DEFAULTSPACK_OPEN_BROWSER": "1",
                    "RUMI_DEFAULTSPACK_SURFACE": "webview",
                    "TOBKIRI_USER_DATA": str(user_data),
                    "RUMI_USER_DATA": str(user_data),
                    "TOBKIRI_HOST_CONTRACT_PATH": str(
                        user_data / "host_contract.json"
                    ),
                },
                clear=True,
            ):
                with patch("core_runtime.pack_api_server.PackAPIServer", return_value=fake_server):
                    with patch.object(desktop_app, "_wait_until_ready", return_value=True):
                        with patch.object(desktop_app, "_wait_until_chat_ready", return_value=True):
                            with patch("defaultspack.native_webview.open_desktop_surface", return_value="webview"):
                                result = desktop_app.main()

        self.assertEqual(result, 0)
        self.assertTrue(fake_server.started)
        self.assertTrue(fake_server.stopped)

    def test_valid_stale_profile_starts_ui_ready_reconfirmation_surface(self):
        from core_runtime.app_lifecycle_manager import (
            get_runtime_readiness,
            reset_runtime_readiness,
        )
        from defaultspack import desktop_app
        from ecosystem.defaultspack.domain.runtime_v4 import (
            ProfileReconfirmationRequired,
        )

        captured: dict[str, object] = {}
        events: list[tuple[str, dict[str, object]]] = []

        class FakeServer:
            def __init__(self, *_args, **kwargs):
                captured.update(kwargs)

            def start(self):
                return None

            def stop(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            user_data = Path(tmp) / "user_data"
            self._activate_defaults(user_data)
            reset_runtime_readiness()
            env = {
                "RUMI_DEFAULTSPACK_OPEN_BROWSER": "1",
                "RUMI_DEFAULTSPACK_SURFACE": "webview",
                "TOBKIRI_USER_DATA": str(user_data),
                "RUMI_USER_DATA": str(user_data),
                "TOBKIRI_HOST_CONTRACT_PATH": str(
                    user_data / "host_contract.json"
                ),
            }
            denial = (
                "legacy activation requires explicit reconfirmation: "
                "Authority Kernel reference is missing for edge test"
            )
            with patch.dict(os.environ, env, clear=True):
                with patch.object(
                    desktop_app,
                    "_restore_active_profile_contracts",
                    side_effect=ProfileReconfirmationRequired(denial),
                ):
                    with patch.object(
                        desktop_app,
                        "_write_launch_event",
                        side_effect=lambda event, **fields: events.append(
                            (event, fields)
                        ),
                    ):
                        with patch(
                            "core_runtime.pack_api_server.PackAPIServer",
                            FakeServer,
                        ):
                            with patch.object(
                                desktop_app, "_wait_until_ready", return_value=True
                            ):
                                with patch.object(
                                    desktop_app,
                                    "_wait_until_chat_ready",
                                    return_value=True,
                                ):
                                    with patch(
                                        "defaultspack.native_webview.open_desktop_surface",
                                        return_value="webview",
                                    ):
                                        result = desktop_app.main()

        self.assertEqual(result, 0)
        self.assertIsNone(captured["dispatch_session"])
        self.assertEqual(captured["contract_bindings"], ())
        readiness = get_runtime_readiness()
        self.assertTrue(readiness["panel_ready"])
        self.assertFalse(readiness["runtime_ready"])
        self.assertEqual(
            readiness["runtime_status"], "profile_reconfirmation_required"
        )
        self.assertEqual(readiness["runtime_error"], denial)
        event = next(
            item for item in events if item[0] == "profile_reconfirmation_required"
        )
        self.assertEqual(event[1]["denial_diagnostic"], denial)

    def test_desktop_app_main_fails_closed_when_server_bind_is_busy(self):
        from defaultspack import desktop_app

        class PortBusyServer:
            def __init__(self, facade=None):
                self.stopped = False

            def start(self):
                raise OSError("address already in use")

            def stop(self):
                self.stopped = True

        fake_server = PortBusyServer()

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "defaultspack-launch.jsonl"
            user_data = Path(tmp) / "user_data"
            self._activate_defaults(user_data)
            env = {
                "DEFAULTS_HTTP_PORT": "8766",
                "RUMI_DEFAULTSPACK_LAUNCH_LOG": str(log_path),
                "RUMI_DEFAULTSPACK_OPEN_BROWSER": "1",
                "RUMI_DEFAULTSPACK_PORT": "8766",
                "TOBKIRI_USER_DATA": str(user_data),
                "RUMI_USER_DATA": str(user_data),
                "TOBKIRI_HOST_CONTRACT_PATH": str(
                    user_data / "host_contract.json"
                ),
            }
            with patch.dict(os.environ, env, clear=True):
                with patch("core_runtime.pack_api_server.PackAPIServer", return_value=fake_server):
                    with patch.object(desktop_app, "_wait_until_ready", return_value=True):
                        with patch.object(desktop_app, "_wait_until_chat_ready", return_value=True):
                            with patch.object(
                                desktop_app,
                                "_port_owner_snapshot",
                                return_value=[{"pid": "123", "command": "python3"}],
                            ):
                                with patch("defaultspack.native_webview.open_desktop_surface", return_value="browser"):
                                    with self.assertRaisesRegex(
                                        OSError, "address already in use"
                                    ):
                                        desktop_app.main()

            self.assertFalse(fake_server.stopped)
            events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

        event_names = [event["event"] for event in events]
        self.assertIn("server_start_oserror", event_names)
        self.assertNotIn("duplicate_launcher_exit", event_names)
        busy_event = next(event for event in events if event["event"] == "server_start_oserror")
        self.assertFalse(busy_event["existing_ready"])
        self.assertTrue(busy_event["own_bind_required"])
        self.assertEqual(busy_event["port_owners"], [{"pid": "123", "command": "python3"}])
        self.assertNotIn("RUMI_API_TOKEN", events[0]["env"])

    def test_wait_until_chat_ready_sleeps_after_unmatched_200_response(self):
        from defaultspack import desktop_app

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, _limit):
                return b"still warming"

        sleeps = []

        with patch.object(desktop_app.urllib.request, "urlopen", return_value=FakeResponse()):
            with patch.object(desktop_app.time, "time", side_effect=[0.0, 0.0, 0.1, 0.3]):
                with patch.object(desktop_app.time, "sleep", side_effect=sleeps.append):
                    result = desktop_app._wait_until_chat_ready("http://localhost:8766/chat", timeout=0.25)

        self.assertFalse(result)
        self.assertEqual(sleeps, [0.2, 0.2])

    def test_managed_pack_root_alias_supports_ecosystem_defaultspack_imports(self):
        from defaultspack import desktop_app

        saved_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == "ecosystem" or name.startswith("ecosystem.defaultspack")
        }
        try:
            for name in list(sys.modules):
                if name == "ecosystem" or name.startswith("ecosystem.defaultspack"):
                    sys.modules.pop(name, None)

            with tempfile.TemporaryDirectory() as tmp:
                pack_root = Path(tmp)
                domain_dir = pack_root / "domain"
                domain_dir.mkdir()
                (domain_dir / "__init__.py").write_text("", encoding="utf-8")
                (domain_dir / "managed_marker.py").write_text(
                    "VALUE = 'managed-defaultspack'\n",
                    encoding="utf-8",
                )

                desktop_app._install_ecosystem_defaultspack_alias(pack_root)
                module = import_module("ecosystem.defaultspack.domain.managed_marker")

            self.assertEqual(module.VALUE, "managed-defaultspack")
        finally:
            for name in list(sys.modules):
                if name == "ecosystem" or name.startswith("ecosystem.defaultspack"):
                    sys.modules.pop(name, None)
            sys.modules.update(saved_modules)

    def test_managed_pack_alias_keeps_sibling_tools_pack_visible(self):
        from defaultspack import desktop_app

        saved_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == "ecosystem" or name.startswith("ecosystem.")
        }
        try:
            for name in list(sys.modules):
                if name == "ecosystem" or name.startswith("ecosystem."):
                    sys.modules.pop(name, None)

            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                app_dir = tmp_path / "app"
                tools_pkg = app_dir / "ecosystem" / "rumi_default_tools_pack"
                tool_dir = tools_pkg / "domain" / "tool"
                tool_dir.mkdir(parents=True)
                (tools_pkg / "__init__.py").write_text("", encoding="utf-8")
                (tools_pkg / "domain" / "__init__.py").write_text("", encoding="utf-8")
                (tool_dir / "__init__.py").write_text("", encoding="utf-8")
                (tool_dir / "marker.py").write_text("VALUE = 'tools-pack'\n", encoding="utf-8")

                pack_root = tmp_path / "user_data" / "packs" / "defaultspack" / "versions" / "2.0.0"
                (pack_root / "domain").mkdir(parents=True)
                (pack_root / "domain" / "__init__.py").write_text("", encoding="utf-8")
                (pack_root / "domain" / "managed_marker.py").write_text(
                    "VALUE = 'managed-defaultspack'\n",
                    encoding="utf-8",
                )

                with patch.dict(os.environ, {"RUMI_APP_DIR": str(app_dir)}, clear=False):
                    desktop_app._install_ecosystem_defaultspack_alias(pack_root)
                    managed = import_module("ecosystem.defaultspack.domain.managed_marker")
                    tools = import_module("ecosystem.rumi_default_tools_pack.domain.tool.marker")

            self.assertEqual(managed.VALUE, "managed-defaultspack")
            self.assertEqual(tools.VALUE, "tools-pack")
        finally:
            for name in list(sys.modules):
                if name == "ecosystem" or name.startswith("ecosystem."):
                    sys.modules.pop(name, None)
            sys.modules.update(saved_modules)

    def test_legacy_pack_alias_keeps_resource_ecosystem_visible(self):
        from defaultspack import desktop_app

        saved_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == "ecosystem" or name.startswith("ecosystem.")
        }
        try:
            for name in list(sys.modules):
                if name == "ecosystem" or name.startswith("ecosystem."):
                    sys.modules.pop(name, None)

            with tempfile.TemporaryDirectory() as tmp:
                app_dir = Path(tmp) / "app"
                ecosystem_dir = app_dir / "ecosystem"
                pack_root = ecosystem_dir / "defaultspack"
                default_domain = pack_root / "domain"
                tools_pkg = ecosystem_dir / "rumi_default_tools_pack"
                tool_dir = tools_pkg / "domain" / "tool"
                default_domain.mkdir(parents=True)
                tool_dir.mkdir(parents=True)
                (default_domain / "__init__.py").write_text("", encoding="utf-8")
                (default_domain / "legacy_marker.py").write_text(
                    "VALUE = 'legacy-defaultspack'\n",
                    encoding="utf-8",
                )
                (tools_pkg / "__init__.py").write_text("", encoding="utf-8")
                (tools_pkg / "domain" / "__init__.py").write_text("", encoding="utf-8")
                (tool_dir / "__init__.py").write_text("", encoding="utf-8")
                (tool_dir / "marker.py").write_text("VALUE = 'legacy-tools-pack'\n", encoding="utf-8")

                with patch.dict(os.environ, {"RUMI_APP_DIR": str(app_dir)}, clear=False):
                    desktop_app._install_ecosystem_defaultspack_alias(pack_root)
                    legacy_default = import_module("ecosystem.defaultspack.domain.legacy_marker")
                    legacy_tools = import_module("ecosystem.rumi_default_tools_pack.domain.tool.marker")

            self.assertEqual(legacy_default.VALUE, "legacy-defaultspack")
            self.assertEqual(legacy_tools.VALUE, "legacy-tools-pack")
        finally:
            for name in list(sys.modules):
                if name == "ecosystem" or name.startswith("ecosystem."):
                    sys.modules.pop(name, None)
            sys.modules.update(saved_modules)


if __name__ == "__main__":
    unittest.main()
