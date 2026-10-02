"""Tests for desktop_app_manager.py — launch_app() argument construction.

Agent L — Wave 2: Verify that launch_app() constructs the correct
Popen arguments after the --command / RUMI_API_TOKEN fix.
"""
from __future__ import annotations

import os
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from core_runtime.desktop_app_manager import DesktopAppManager
import core_runtime.desktop_app_manager as desktop_app_manager
from tests.conformance_support.host_contract import host_contract


@pytest.fixture
def manager(tmp_path):
    """Create a DesktopAppManager with a temporary repo dir."""
    repo_dir = str(tmp_path / "tobkiri_runtime")
    os.makedirs(os.path.join(repo_dir, "user_data", "apps"), exist_ok=True)
    return DesktopAppManager(repo_dir=repo_dir)


@pytest.fixture
def sample_meta():
    """Sample app metadata as saved by register_app()."""
    return {
        "pack_id": "test-pack-001",
        "command": "python app.py --verbose",
        "pack_dir": "/tmp/packs/test-pack-001",
        "pack_shell": "/usr/local/bin/pack-shell",
        "requires_api_token": True,
        "window": {"title": "Test App"},
        "env": {"CUSTOM_VAR": "hello"},
        "working_dir": "/tmp/packs/test-pack-001",
        "platforms": [],
    }


def test_register_app_stores_metadata_under_rumi_user_data(tmp_path, monkeypatch):
    """Launcher-managed registrations must not write into the app bundle."""
    repo_dir = tmp_path / "bundle" / "tobkiri_runtime"
    user_data = tmp_path / "Library" / "Application Support" / "Tobkiri"
    pack_shell = tmp_path / "pack-shell"
    pack_shell.write_text("#!/bin/sh\n", encoding="utf-8")
    pack_shell.chmod(0o755)
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_PACK_SHELL_PATH", str(pack_shell))
    manager = DesktopAppManager(repo_dir=str(repo_dir))

    with mock.patch.object(manager, "_create_shortcut", return_value="/tmp/Test.app"):
        result = manager.register_app(
            "defaultspack",
            {"command": "python desktop_app.py"},
            str(repo_dir / "ecosystem" / "defaultspack"),
        )

    assert result["success"] is True
    assert (user_data / "apps" / "defaultspack.json").is_file()
    assert not (repo_dir / "user_data" / "apps" / "defaultspack.json").exists()


class TestLaunchAppArguments:
    """Verify that launch_app() constructs the correct Popen arguments."""

    @mock.patch("subprocess.Popen")
    @mock.patch("os.path.isfile", return_value=True)
    def test_launch_app_passes_run_subcommand_and_command_flag(
        self, mock_isfile, mock_popen, manager, sample_meta
    ):
        """Popen should be called with:
        [pack_shell, 'run', pack_id, '--command', command]
        """
        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        manager._load_meta = mock.MagicMock(return_value=sample_meta)

        from core_runtime.host_contract import bind_host_contract

        with mock.patch.dict(os.environ, {"RUMI_API_TOKEN": "ambient-token"}):
            with bind_host_contract(
                host_contract(
                    profile_id="default",
                    values={"desktop_api_token": "secret-token-xyz"},
                )
            ):
                result = manager.launch_app("test-pack-001")

        assert result["success"] is True
        assert result["status"] == "launched"

        call_args = mock_popen.call_args
        cmd_list = call_args[0][0]  # first positional arg to Popen
        assert cmd_list == [
            "/usr/local/bin/pack-shell",
            "run",
            "test-pack-001",
            "--command",
            "python app.py --verbose",
            "--working-dir",
            "/tmp/packs/test-pack-001",
        ]

    @mock.patch("subprocess.Popen")
    @mock.patch("os.path.isfile", return_value=True)
    def test_launch_app_passes_rumi_api_token_in_env(
        self, mock_isfile, mock_popen, manager, sample_meta
    ):
        """RUMI_API_TOKEN should appear in the env dict passed to Popen."""
        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        manager._load_meta = mock.MagicMock(return_value=sample_meta)

        from core_runtime.host_contract import bind_host_contract

        with mock.patch.dict(os.environ, {"RUMI_API_TOKEN": "ambient-token"}):
            with bind_host_contract(
                host_contract(
                    profile_id="default",
                    values={"desktop_api_token": "secret-token-xyz"},
                )
            ):
                result = manager.launch_app("test-pack-001")

        assert result["success"] is True
        call_kwargs = mock_popen.call_args[1]
        env = call_kwargs.get("env")
        assert env is not None
        assert env.get("RUMI_API_TOKEN") == "secret-token-xyz"
        assert "RUMI_DEFAULTSPACK_LOCAL_TOKEN" not in env

    @mock.patch("subprocess.Popen")
    @mock.patch("os.path.isfile", return_value=True)
    def test_launch_app_with_env_overrides_pack_env(
        self, mock_isfile, mock_popen, manager, sample_meta
    ):
        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        manager._load_meta = mock.MagicMock(return_value=sample_meta)

        result = manager.launch_app_with_env(
            "test-pack-001",
            api_token="issued-token",
            env_overrides={"CUSTOM_VAR": "override", "RUMI_DEFAULTSPACK_SURFACE": "browser"},
        )

        assert result["success"] is True
        env = mock_popen.call_args.kwargs["env"]
        assert env["CUSTOM_VAR"] == "override"
        assert env["RUMI_DEFAULTSPACK_SURFACE"] == "browser"

    @mock.patch("os.path.isfile", return_value=True)
    def test_launch_app_errors_when_command_is_empty(
        self, mock_isfile, manager, sample_meta
    ):
        """launch_app() should return an error if command is empty."""
        meta_no_cmd = dict(sample_meta)
        meta_no_cmd["command"] = ""

        manager._load_meta = mock.MagicMock(return_value=meta_no_cmd)

        result = manager.launch_app("test-pack-001")

        assert result["success"] is False
        assert "No command" in result["error"]

    @mock.patch("os.path.isfile", return_value=True)
    def test_launch_app_errors_when_command_is_none(
        self, mock_isfile, manager, sample_meta
    ):
        """launch_app() should return an error if command key is missing."""
        meta_no_cmd = dict(sample_meta)
        del meta_no_cmd["command"]

        manager._load_meta = mock.MagicMock(return_value=meta_no_cmd)

        result = manager.launch_app("test-pack-001")

        assert result["success"] is False
        assert "No command" in result["error"]

    def test_launch_app_errors_when_not_registered(self, manager):
        """launch_app() should return an error for unregistered pack_id."""
        result = manager.launch_app("nonexistent-pack")

        assert result["success"] is False
        assert "not registered" in result["error"].lower()

    def test_resolve_pack_shell_prefers_bundled_runtime_copy(self, tmp_path):
        """Bundled Tauri runtime should resolve app/bundled/pack-shell."""
        repo_dir = tmp_path / "tobkiri_runtime"
        bundled_shell = repo_dir / "bundled" / desktop_app_manager._pack_shell_binary_name()
        bundled_shell.parent.mkdir(parents=True)
        bundled_shell.write_text("#!/bin/sh\n", encoding="utf-8")
        bundled_shell.chmod(0o755)

        env_clean = {
            k: v
            for k, v in os.environ.items()
            if k not in {"RUMI_PACK_SHELL_PATH", "PATH"}
        }
        with mock.patch.object(desktop_app_manager, "_default_repo_dir", return_value=str(repo_dir)):
            with mock.patch.dict(os.environ, env_clean, clear=True):
                assert desktop_app_manager._resolve_pack_shell_path() == str(bundled_shell)

    @mock.patch("subprocess.Popen")
    def test_launch_app_lazily_registers_repo_local_pack(
        self, mock_popen, tmp_path
    ):
        """Viewer launch can start a repo-local desktop_app before metadata exists."""
        repo_dir = tmp_path / "tobkiri_runtime"
        pack_dir = repo_dir / "ecosystem" / "autopack"
        pack_dir.mkdir(parents=True)
        (pack_dir / "ecosystem.json").write_text(
            json.dumps(
                {
                    "pack_id": "autopack",
                    "desktop_app": {
                        "command": "python app.py",
                        "env": {"AUTOPACK_PORT": "9999"},
                    },
                }
            ),
            encoding="utf-8",
        )

        pack_shell = tmp_path / "pack-shell"
        pack_shell.write_text("#!/bin/sh\n", encoding="utf-8")
        pack_shell.chmod(0o755)

        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        manager = DesktopAppManager(repo_dir=str(repo_dir))
        with mock.patch.dict(os.environ, {"RUMI_PACK_SHELL_PATH": str(pack_shell)}):
            with mock.patch.object(manager, "_create_shortcut", return_value=str(tmp_path / "Autopack.app")):
                result = manager.launch_app("autopack", api_token="issued-token")

        assert result["success"] is True
        assert result["launch_mode"] == "direct"
        meta_path = repo_dir / "user_data" / "apps" / "autopack.json"
        assert meta_path.is_file()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["pack_dir"] == str(pack_dir)

        cmd_list = mock_popen.call_args[0][0]
        assert cmd_list == [desktop_app_manager._runtime_python_for_app(), "app.py"]
        env = mock_popen.call_args.kwargs["env"]
        assert env["RUMI_API_TOKEN"] == "issued-token"
        assert "RUMI_DEFAULTSPACK_LOCAL_TOKEN" not in env
        assert env["RUMI_TOKEN"] == "issued-token"
        assert env["AUTOPACK_PORT"] == "9999"
        assert str(Path(sys.executable).resolve().parent) in env["PATH"]

    @mock.patch("subprocess.Popen")
    def test_launch_app_rejects_traversal_pack_id_before_lazy_registration(
        self, mock_popen, tmp_path
    ):
        """Lazy registration must not resolve ecosystem.json outside repo ecosystem/."""
        repo_dir = tmp_path / "tobkiri_runtime"
        forged_dir = repo_dir / "user_data" / "evil"
        forged_dir.mkdir(parents=True)
        (forged_dir / "ecosystem.json").write_text(
            json.dumps(
                {
                    "pack_id": "evil",
                    "desktop_app": {"command": "python evil.py"},
                }
            ),
            encoding="utf-8",
        )

        manager = DesktopAppManager(repo_dir=str(repo_dir))
        result = manager.launch_app("../user_data/evil", api_token="issued-token")

        assert result["success"] is False
        assert "Invalid pack_id" in result["error"]
        assert not (repo_dir / "user_data" / "apps" / "evil.json").exists()
        mock_popen.assert_not_called()

    @mock.patch("subprocess.Popen")
    def test_launch_app_rejects_lazy_registration_symlink_escape(
        self, mock_popen, tmp_path
    ):
        """A syntactically valid pack_id still cannot point outside ecosystem/ via symlink."""
        repo_dir = tmp_path / "tobkiri_runtime"
        ecosystem_dir = repo_dir / "ecosystem"
        ecosystem_dir.mkdir(parents=True)
        outside_pack = tmp_path / "outside_pack"
        outside_pack.mkdir()
        (outside_pack / "ecosystem.json").write_text(
            json.dumps(
                {
                    "pack_id": "linkpack",
                    "desktop_app": {"command": "python evil.py"},
                }
            ),
            encoding="utf-8",
        )
        (ecosystem_dir / "linkpack").symlink_to(outside_pack, target_is_directory=True)

        manager = DesktopAppManager(repo_dir=str(repo_dir))
        result = manager.launch_app("linkpack", api_token="issued-token")

        assert result["success"] is False
        assert "Path traversal" in result["error"]
        assert not (repo_dir / "user_data" / "apps" / "linkpack.json").exists()
        mock_popen.assert_not_called()

    @mock.patch("subprocess.Popen")
    @mock.patch("os.path.isfile", return_value=True)
    def test_launch_app_includes_custom_env_vars(
        self, mock_isfile, mock_popen, manager, sample_meta
    ):
        """Custom env vars from meta should be present in Popen env."""
        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        manager._load_meta = mock.MagicMock(return_value=sample_meta)

        from core_runtime.host_contract import bind_host_contract

        with mock.patch.dict(os.environ, {"RUMI_API_TOKEN": "ambient-token"}):
            with bind_host_contract(
                host_contract(
                    profile_id="default",
                    values={"desktop_api_token": "secret-token-xyz"},
                )
            ):
                result = manager.launch_app("test-pack-001")

        assert result["success"] is True
        call_kwargs = mock_popen.call_args[1]
        env = call_kwargs.get("env")
        assert env.get("CUSTOM_VAR") == "hello"
        assert env.get("RUMI_PACK_ID") == "test-pack-001"

    @mock.patch("subprocess.Popen")
    @mock.patch("os.path.isfile", return_value=True)
    def test_launch_app_errors_without_rumi_api_token_env(
        self, mock_isfile, mock_popen, manager, sample_meta
    ):
        """Desktop app launch should fail fast when RUMI_API_TOKEN is missing."""
        manager._load_meta = mock.MagicMock(return_value=sample_meta)

        env_clean = {k: v for k, v in os.environ.items() if k != "RUMI_API_TOKEN"}
        with mock.patch.dict(os.environ, env_clean, clear=True):
            result = manager.launch_app("test-pack-001")

        assert result["success"] is False
        assert "RUMI_API_TOKEN" in result["error"]
        mock_popen.assert_not_called()
